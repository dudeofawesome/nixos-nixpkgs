import os
from datetime import datetime
from pathlib import Path
from subprocess import PIPE, CalledProcessError
from typing import Final

from .models import (
    Action,
    Flake,
    Generation,
    GenerationJson,
    NRError,
    Profile,
    Remote,
)
from .process import run_wrapper
from .utils import Args, dict_to_flags, info

FLAKE_FLAGS: Final = ["--extra-experimental-features", "nix-command flakes"]


def copy_closure(
    closure: Path,
    target_host: Remote | None,
    **copy_flags: Args,
) -> None:
    host = target_host
    if not host:
        return

    run_wrapper(
        [
            "nix-copy-closure",
            *dict_to_flags(copy_flags),
            "--to",
            host.host,
            closure,
        ],
        extra_env={"NIX_SSHOPTS": " ".join(host.opts)},
    )


def edit(flake: Flake | None, **flake_flags: Args) -> None:
    "Try to find and open NixOS configuration file in editor."
    if flake:
        run_wrapper(
            [
                "nix",
                *FLAKE_FLAGS,
                "edit",
                *dict_to_flags(flake_flags),
                "--",
                str(flake),
            ],
            check=False,
        )
    else:
        if flake_flags:
            raise NRError("'edit' does not support extra Nix flags")
        nixos_config = Path(
            os.getenv("NIXOS_CONFIG")
            or run_wrapper(
                ["nix-instantiate", "--find-file", "nixos-config"],
                stdout=PIPE,
                check=False,
            ).stdout.strip()
            or "/etc/nixos/default.nix"
        )
        if nixos_config.is_dir():
            nixos_config /= "default.nix"

        if nixos_config.exists():
            run_wrapper([os.getenv("EDITOR", "nano"), nixos_config], check=False)
        else:
            raise NRError("cannot find NixOS config file")


def find_file(file: str, **nix_flags: Args) -> Path | None:
    "Find classic Nixpkgs location."
    r = run_wrapper(
        ["nix-instantiate", "--find-file", file, *dict_to_flags(nix_flags)],
        stdout=PIPE,
        check=False,
    )
    if r.returncode:
        return None
    return Path(r.stdout.strip())


def get_nixpkgs_rev(nixpkgs_path: Path | None) -> str | None:
    """Get Nixpkgs path as a Git revision.

    Can be used to generate `.version-suffix` file."""
    if not nixpkgs_path:
        return None

    try:
        # Get current revision
        r = run_wrapper(
            ["git", "-C", nixpkgs_path, "rev-parse", "--short", "HEAD"],
            check=False,
            stdout=PIPE,
        )
    except FileNotFoundError:
        # Git is not included in the closure so we need to check
        info(f"warning: Git not found; cannot figure out revision of '{nixpkgs_path}'")
        return None

    if rev := r.stdout.strip():
        # Check if repo is dirty
        if run_wrapper(
            ["git", "-C", nixpkgs_path, "diff", "--quiet"],
            check=False,
        ).returncode:
            rev += "M"
        return f".git.{rev}"
    else:
        return None


def _parse_generation_from_nix_store(path: Path, profile: Profile) -> Generation:
    entry_id = path.name.split("-")[1]
    current = path.name == profile.path.readlink().name
    timestamp = datetime.fromtimestamp(path.stat().st_ctime).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return Generation(
        id=int(entry_id),
        timestamp=timestamp,
        current=current,
    )


def _parse_generation_from_nix_env(line: str) -> Generation:
    parts = line.split()

    entry_id = parts[0]
    timestamp = f"{parts[1]} {parts[2]}"
    current = "(current)" in parts

    return Generation(
        id=int(entry_id),
        timestamp=timestamp,
        current=current,
    )


def get_generations(
    profile: Profile,
    target_host: Remote | None = None,
    using_nix_env: bool = False,
    sudo: bool = False,
) -> list[Generation]:
    """Get all NixOS generations from profile.

    Includes generation ID (e.g.: 1, 2), timestamp (e.g.: when it was created)
    and if this is the current active profile or not.

    If `lock_profile = True` this command will need root to run successfully.
    """
    if not profile.path.exists():
        raise NRError(f"no profile '{profile.name}' found")

    result = []
    if using_nix_env:
        # Using `nix-env --list-generations` needs root to lock the profile
        # TODO: do we actually need to lock profile for e.g.: rollback?
        # https://github.com/NixOS/nix/issues/5144
        r = run_wrapper(
            ["nix-env", "-p", profile.path, "--list-generations"],
            stdout=PIPE,
            remote=target_host,
            sudo=sudo,
        )
        for line in r.stdout.splitlines():
            result.append(_parse_generation_from_nix_env(line))
    else:
        assert not target_host, "target_host is not supported when using_nix_env=False"
        for p in profile.path.parent.glob("system-*-link"):
            result.append(_parse_generation_from_nix_store(p, profile))
    return sorted(result, key=lambda d: d.id)


def list_generations(profile: Profile) -> list[GenerationJson]:
    """Get all NixOS generations from profile, including extra information.

    Includes OS information like the commit, kernel version, configuration
    revision and specialisations.

    Will be formatted in a way that is expected by the output of
    `nixos-rebuild list-generations --json`.
    """
    generations = get_generations(profile)
    result = []
    for generation in reversed(generations):
        generation_path = (
            profile.path.parent / f"{profile.path.name}-{generation.id}-link"
        )
        try:
            nixos_version = (generation_path / "nixos-version").read_text().strip()
        except IOError:
            nixos_version = "Unknown"
        try:
            kernel_version = next(
                (generation_path / "kernel-modules/lib/modules").iterdir()
            ).name
        except IOError:
            kernel_version = "Unknown"
        specialisations = [
            s.name for s in (generation_path / "specialisation").glob("*") if s.is_dir()
        ]
        try:
            configuration_revision = run_wrapper(
                [generation_path / "sw/bin/nixos-version", "--configuration-revision"],
                capture_output=True,
            ).stdout.strip()
        except (CalledProcessError, IOError):
            configuration_revision = "Unknown"

        result.append(
            GenerationJson(
                generation=generation.id,
                date=generation.timestamp,
                nixosVersion=nixos_version,
                kernelVersion=kernel_version,
                configurationRevision=configuration_revision,
                specialisations=specialisations,
                current=generation.current,
            )
        )

    return result


def nixos_build(
    attr: str,
    pre_attr: str | None,
    file: str | None,
    **nix_flags: Args,
) -> Path:
    """Build NixOS attribute using classic Nix.

    It will by default build  `<nixpkgs/nixos>` with `attr`, however it
    optionally supports building from an external file and custom attributes
    paths.

    Returns the built attribute as path.
    """
    if pre_attr or file:
        run_args = [
            "nix-build",
            file or "default.nix",
            "--attr",
            f"{'.'.join(x for x in [pre_attr, attr] if x)}",
        ]
    else:
        run_args = ["nix-build", "<nixpkgs/nixos>", "--attr", attr]
    run_args += dict_to_flags(nix_flags)
    r = run_wrapper(run_args, stdout=PIPE)
    return Path(r.stdout.strip())


def nixos_build_flake(
    attr: str,
    flake: Flake,
    **flake_flags: Args,
) -> Path:
    """Build NixOS attribute using Flakes.

    Returns the built attribute as path.
    """
    run_args = [
        "nix",
        *FLAKE_FLAGS,
        "build",
        "--print-out-paths",
        f"{flake}.config.system.build.{attr}",
        *dict_to_flags(flake_flags),
    ]
    r = run_wrapper(run_args, stdout=PIPE)
    return Path(r.stdout.strip())


def rollback(profile: Profile, target_host: Remote | None, sudo: bool) -> Path:
    "Rollback Nix profile, like one created by `nixos-rebuild switch`."
    run_wrapper(
        ["nix-env", "--rollback", "-p", profile.path],
        remote=target_host,
        sudo=sudo,
    )
    # Rollback config PATH is the own profile
    return profile.path


def rollback_temporary_profile(
    profile: Profile,
    target_host: Remote | None,
    sudo: bool,
) -> Path | None:
    "Rollback a temporary Nix profile, like one created by `nixos-rebuild test`."
    generations = get_generations(
        profile,
        target_host=target_host,
        using_nix_env=True,
        sudo=sudo,
    )
    previous_gen_id = None
    for generation in generations:
        if not generation.current:
            previous_gen_id = generation.id

    if previous_gen_id:
        return profile.path.parent / f"{profile.name}-{previous_gen_id}-link"
    else:
        return None


def set_profile(
    profile: Profile,
    path_to_config: Path,
    target_host: Remote | None,
    sudo: bool,
) -> None:
    "Set a path as the current active Nix profile."
    run_wrapper(
        ["nix-env", "-p", profile.path, "--set", path_to_config],
        remote=target_host,
        sudo=sudo,
    )


def switch_to_configuration(
    path_to_config: Path,
    action: Action,
    target_host: Remote | None,
    sudo: bool,
    install_bootloader: bool = False,
    specialisation: str | None = None,
) -> None:
    """Call `<config>/bin/switch-to-configuration <action>`.

    Expects a built path to run, like one generated with `nixos_build` or
    `nixos_build_flake` functions.
    """
    if specialisation:
        if action not in (Action.SWITCH, Action.TEST):
            raise NRError(
                "'--specialisation' can only be used with 'switch' and 'test'"
            )
        path_to_config = path_to_config / f"specialisation/{specialisation}"

        if not path_to_config.exists():
            raise NRError(f"specialisation not found: {specialisation}")

    run_wrapper(
        [path_to_config / "bin/switch-to-configuration", str(action)],
        extra_env={"NIXOS_INSTALL_BOOTLOADER": "1" if install_bootloader else "0"},
        remote=target_host,
        sudo=sudo,
    )


def upgrade_channels(all: bool = False) -> None:
    """Upgrade channels for classic Nix.

    It will either upgrade just the `nixos` channel (including any channel
    that has a `.update-on-nixos-rebuild` file) or all.
    """
    for channel_path in Path("/nix/var/nix/profiles/per-user/root/channels/").glob("*"):
        if (
            all
            or channel_path.name == "nixos"
            or (channel_path / ".update-on-nixos-rebuild").exists()
        ):
            run_wrapper(["nix-channel", "--update", channel_path.name], check=False)
