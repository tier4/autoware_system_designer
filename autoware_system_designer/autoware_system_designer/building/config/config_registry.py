# Copyright 2026 TIER IV, inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Type

from ...exceptions import (
    FormatVersionError,
    ModuleConfigurationError,
    NodeConfigurationError,
    ParameterConfigurationError,
    ValidationError,
)
from ...file_io.source_location import SourceLocation, format_source, source_from_config
from ...parsing.config import (
    Config,
    ConfigSubType,
    ConfigType,
    ModuleConfig,
    NodeConfig,
    ParameterSetConfig,
    SystemConfig,
)
from ...parsing.loaders.data_parser import ConfigParser
from ...parsing.loaders.data_validator import entity_name_decode
from ...utils.format_version import check_format_version
from ..resolution.variant_resolver import (
    ModuleVariantResolver,
    NodeVariantResolver,
    SystemVariantResolver,
    VariantResolver,
)

logger = logging.getLogger(__name__)


def _format_mismatch_hint(mismatch_files: list) -> str:
    """Build a human-readable hint listing files with version mismatches.

    Each entry in *mismatch_files* is ``(file_path, file_version, supported_version)``.
    """
    lines = []
    for entry in mismatch_files:
        fpath, file_ver, supported_ver = entry
        lines.append(f"  {fpath}  (file: {file_ver}, supported: {supported_ver})")
    file_list = "\n".join(lines)
    return (
        f"Note: the following design files use a newer minor format version "
        f"than this tool supports.  This may have contributed to the error:\n"
        f"{file_list}"
    )


class ConfigRegistry:
    """Collection for managing multiple entity data structures with efficient lookup methods."""

    def __init__(
        self,
        config_yaml_file_paths: List[str],
        package_paths: Dict[str, str] = None,
        file_package_map: Dict[str, str] = None,
        workspace_config: List[Dict[str, Any]] = None,
        strict: bool = False,
    ):
        # Replace list with dict as primary storage
        self.entities: Dict[str, Config] = {}  # full_name → Config
        self._type_map: Dict[str, Dict[str, Config]] = {
            ConfigType.NODE: {},
            ConfigType.MODULE: {},
            ConfigType.PARAMETER_SET: {},
            ConfigType.SYSTEM: {},
        }
        self.package_paths = package_paths or {}
        self.file_package_map = file_package_map or {}
        self._package_source_paths: Dict[str, Optional[str]] = {}
        # Name of the package currently being built/exported (deployment package).
        # When set, build-time source fallbacks should be restricted to this package only.
        self.deployment_package_name: Optional[str] = None

        # Workspace provider resolution map: provider -> "source" | "installed"
        self._provider_resolution_map: Dict[str, str] = {}
        if workspace_config:
            for entry in workspace_config:
                if isinstance(entry, dict) and "provider" in entry and "resolution" in entry:
                    self._provider_resolution_map[entry["provider"]] = entry["resolution"]

        # Track files whose minor format version is newer than the tool supports.
        # Populated during _load_entities; surfaced to the user when the build fails.
        self.minor_version_mismatch_files: List[str] = []

        self.strict = strict

        # full_name → every config declaring that name, in load order.
        # The workspace-wide scan records collisions without failing; they are
        # reported only when the deployed system actually uses the entity.
        self.duplicate_entities: Dict[str, List[Config]] = {}
        self._reported_duplicates: Set[str] = set()

        self.parser = ConfigParser()
        self._load_entities(config_yaml_file_paths)

    def _load_entities(self, config_yaml_file_paths: List[str]) -> None:
        """Load entities from configuration files."""
        from ...parsing.loaders.yaml_parser import yaml_parser as _yaml_parser

        for file_path in config_yaml_file_paths:
            logger.debug(f"Loading entity from: {file_path}")

            # ── format-version gate ──────────────────────────────────
            try:
                raw_config = _yaml_parser.load_config(file_path)
            except Exception:
                raw_config = None  # let parse_entity_file report the real error

            if isinstance(raw_config, dict):
                raw_ver = raw_config.get("autoware_system_design_format")
                ver_result = check_format_version(raw_ver)
                if not ver_result.compatible:
                    # Major version mismatch → stop immediately
                    src = SourceLocation(file_path=Path(file_path))
                    raise FormatVersionError(f"{ver_result.message}{format_source(src)}")
                if ver_result.minor_newer:
                    # Minor version newer than tool → warn and track
                    src = SourceLocation(file_path=Path(file_path))
                    logger.warning(f"{ver_result.message}{format_source(src)}")
                    self.minor_version_mismatch_files.append(
                        (file_path, str(ver_result.file_version), str(ver_result.supported_version))
                    )
            # ─────────────────────────────────────────────────────────

            try:
                entity_data = self.parser.parse_entity_file(file_path)

                # Set package name if available
                if entity_data.file_path and str(entity_data.file_path) in self.file_package_map:
                    entity_data.package = self.file_package_map[str(entity_data.file_path)]

                # For node entities, resolve the provider against the workspace config
                if isinstance(entity_data, NodeConfig) and entity_data.package_provider:
                    resolution = self._provider_resolution_map.get(entity_data.package_provider)
                    if resolution:
                        entity_data.package_resolution = resolution

                # Record duplicates and keep the entity already registered.
                if entity_data.full_name in self.entities:
                    existing = self.entities[entity_data.full_name]
                    candidates = self.duplicate_entities.setdefault(entity_data.full_name, [existing])
                    candidates.append(entity_data)
                    logger.info(
                        f"Duplicate entity '{entity_data.full_name}' encountered: {entity_data.file_path} "
                        f"(currently using {existing.file_path}; may be overridden by deployment package)"
                    )
                    continue

                # Add to collections
                self.entities[entity_data.full_name] = entity_data
                self._type_map[entity_data.entity_type][entity_data.name] = entity_data

            except Exception as e:
                src = SourceLocation(file_path=Path(file_path))
                logger.error(f"Failed to load entity from {file_path}: {e}{format_source(src)}")

                # If any files loaded so far had a newer minor format
                # version, surface that alongside the real error so the
                # user knows it may be related.
                if self.minor_version_mismatch_files:
                    hint = _format_mismatch_hint(self.minor_version_mismatch_files)
                    raise type(e)(f"{e}\n{hint}") from e
                raise

    def get(self, name: str, default=None) -> Optional[Config]:
        """Get entity by name with default value."""
        return self.entities.get(name, default)

    def resolve_duplicates(self) -> Set[str]:
        """Repoint duplicated entities at the deployment package's copy.

        Requires deployment_package_name to be final. Returns the full names whose
        registered entity changed.
        """
        swapped: Set[str] = set()
        if not self.deployment_package_name:
            return swapped

        for full_name, candidates in self.duplicate_entities.items():
            registered = self.entities.get(full_name)
            preferred = next(
                (c for c in candidates if c.package == self.deployment_package_name),
                None,
            )
            if preferred is None or preferred is registered:
                continue
            self.entities[full_name] = preferred
            self._type_map[preferred.entity_type][preferred.name] = preferred
            swapped.add(full_name)

        return swapped

    def _check_duplicate_use(self, entity: Config) -> None:
        """Report a duplicated entity at the point the deployed system uses it."""
        candidates = self.duplicate_entities.get(entity.full_name)
        if not candidates or entity.full_name in self._reported_duplicates:
            return
        self._reported_duplicates.add(entity.full_name)

        lines = [
            f"  {'->' if c is entity else '  '} {c.file_path}" + (f"  [{c.package}]" if c.package else "")
            for c in candidates
        ]
        message = (
            f"Duplicate entity '{entity.full_name}' is used by this deployment; "
            f"'->' marks the definition in use:\n" + "\n".join(lines)
        )
        src = format_source(source_from_config(entity, "/name"))
        if self.strict:
             raise ValidationError(f"{message}{src}")
        logger.warning(f"{message}{src}")

    def _get_entity_with_base(
        self,
        name: str,
        config_type: str,
        error_cls: Type[Exception],
        resolver_cls: Optional[Type[VariantResolver]] = None,
        recursive_getter: Optional[Callable[[str], Config]] = None,
    ) -> Config:
        """
        Generic method to get an entity and resolve base/variant if applicable.
        """
        entity = self._type_map[config_type].get(name)

        # If not found, try decoding the name (e.g. MyNode.node -> MyNode)
        if entity is None and "." in name:
            try:
                decoded_name, entity_type = entity_name_decode(name)
                if entity_type == config_type:
                    entity = self._type_map[config_type].get(decoded_name)
            except ValidationError:
                pass

        if entity is None:
            available = list(self._type_map[config_type].keys())
            raise error_cls(f"{config_type.capitalize()} '{name}' not found. Available {config_type}s: {available}")

        self._check_duplicate_use(entity)

        if entity.sub_type == ConfigSubType.VARIANT:
            if not resolver_cls or not recursive_getter:
                # Variant requested but no resolver provided, return as is (or could raise error)
                return entity

            # Get parent name
            base_target = entity.base
            if not base_target:
                # Should have been validated, but fallback
                return entity

            # Resolve parent (recursive)
            parent = recursive_getter(base_target)

            # Create a deep copy of the parent to serve as the base for this entity
            # This ensures we don't modify the parent object
            resolved_entity = copy.deepcopy(parent)

            # Update the identity of the resolved entity to match the current entity
            resolved_entity.name = entity.name
            resolved_entity.full_name = entity.full_name
            resolved_entity.file_path = entity.file_path
            resolved_entity.package = entity.package
            resolved_entity.sub_type = entity.sub_type
            resolved_entity.config = entity.config  # Keep original config with overrides

            # Apply overrides from this entity's config
            resolver = resolver_cls()
            resolver.resolve(resolved_entity, entity.config)

            return resolved_entity

        return entity

    # Enhanced methods for type-safe entity access
    def get_node(self, name: str) -> NodeConfig:
        """Get a node entity by name."""
        return self._get_entity_with_base(
            name, ConfigType.NODE, NodeConfigurationError, NodeVariantResolver, self.get_node
        )

    def get_module(self, name: str) -> ModuleConfig:
        """Get a module entity by name."""
        return self._get_entity_with_base(
            name,
            ConfigType.MODULE,
            ModuleConfigurationError,
            ModuleVariantResolver,
            self.get_module,
        )

    def get_parameter_set(self, name: str) -> ParameterSetConfig:
        """Get a parameter set entity by name."""
        return self._get_entity_with_base(name, ConfigType.PARAMETER_SET, ParameterConfigurationError)

    def get_system(self, name: str) -> SystemConfig:
        """Get an system entity by name. Resolves base/variant if applicable."""
        return self._get_entity_with_base(
            name,
            ConfigType.SYSTEM,
            ValidationError,  # System uses ValidationError in original code, keeping it
            SystemVariantResolver,
            self.get_system,
        )

    def get_entity_by_type(self, name: str, entity_type: str) -> Config:
        """Get an entity by name and type."""
        if entity_type == ConfigType.NODE:
            return self.get_node(name)
        elif entity_type == ConfigType.MODULE:
            return self.get_module(name)
        elif entity_type == ConfigType.PARAMETER_SET:
            return self.get_parameter_set(name)
        elif entity_type == ConfigType.SYSTEM:
            return self.get_system(name)
        else:
            raise ValidationError(f"Unknown entity type: {entity_type}")

    def get_package_path(self, package_name: str) -> Optional[str]:
        """Get package path by package name."""
        return self.package_paths.get(package_name)

    def get_package_source_path(self, package_name: str) -> Optional[str]:
        """Best-effort lookup of a package's *source* directory.

        This is intentionally independent from the install/share path stored in package_paths.
        It is used to avoid false negatives during build-time checks, where install/share may
        not yet contain installed resources (e.g., config/*.yaml).
        """
        if not package_name:
            return None
        if package_name in self._package_source_paths:
            return self._package_source_paths[package_name]

        # Find any design/config yaml that belongs to this package, then walk up to package.xml.
        for file_path, pkg in self.file_package_map.items():
            if pkg != package_name:
                continue
            try:
                current = Path(file_path).resolve().parent
                while True:
                    if (current / "package.xml").exists():
                        self._package_source_paths[package_name] = str(current)
                        return self._package_source_paths[package_name]
                    if current.parent == current:
                        break
                    current = current.parent
            except Exception:
                continue

        # Cache negative result to avoid repeated scans.
        self._package_source_paths[package_name] = None
        return None

    def get_provider_resolution(self, provider: str) -> Optional[str]:
        """Get the resolution type for a given provider.

        Args:
            provider: The provider identifier (e.g., 'autoware', 'ros', 'dummy').

        Returns:
            'source' if the provider's packages are built from source in the workspace,
            'installed' if they are pre-built library packages,
            or None if the provider is not in the workspace config.
        """
        return self._provider_resolution_map.get(provider)
