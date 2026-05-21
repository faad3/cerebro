#!/bin/bash
# Create a 'cerebro' user whose UID/GID matches the owner of the mounted
# ~/.claude directory. This lets the orchestrator's claude process read
# the host user's credentials and config.

CLAUDE_DIR="/home/cerebro/.claude"
if [ -d "$CLAUDE_DIR" ]; then
    TARGET_UID=$(stat -c %u "$CLAUDE_DIR")
    TARGET_GID=$(stat -c %g "$CLAUDE_DIR")

    # Remove the build-time cerebro user if it exists with wrong UID.
    if id cerebro &>/dev/null; then
        CURRENT_UID=$(id -u cerebro)
        if [ "$CURRENT_UID" != "$TARGET_UID" ]; then
            userdel cerebro 2>/dev/null
            groupdel cerebro 2>/dev/null
        fi
    fi

    # Create group + user with the target UID/GID if they don't exist.
    if ! getent group "$TARGET_GID" &>/dev/null; then
        groupadd -g "$TARGET_GID" cerebro 2>/dev/null
    fi
    if ! id -u "$TARGET_UID" &>/dev/null; then
        GRP_NAME=$(getent group "$TARGET_GID" | cut -d: -f1)
        useradd -u "$TARGET_UID" -g "$GRP_NAME" -d /home/cerebro -s /bin/bash -M cerebro 2>/dev/null
    fi

    # Fix home directory ownership (useradd at build time created it as uid 1000,
    # but runtime UID from the mounted .claude/ may differ).
    chown "$TARGET_UID:$TARGET_GID" /home/cerebro

    echo "entrypoint: orchestrator user uid=$TARGET_UID gid=$TARGET_GID"

    # Copy .claude.json into the right place with correct ownership.
    # (Docker file bind mounts don't respect UID mapping well.)
    if [ -f /mnt/host-claude-config.json ]; then
        cp /mnt/host-claude-config.json /home/cerebro/.claude.json
        chown "$TARGET_UID:$TARGET_GID" /home/cerebro/.claude.json
        chmod 600 /home/cerebro/.claude.json
        echo "entrypoint: copied .claude.json → /home/cerebro/.claude.json"
    fi

    # Fix ownership of .claude/ dir entries that might be root-owned from builds.
    chown -R "$TARGET_UID:$TARGET_GID" /home/cerebro/.claude 2>/dev/null || true
fi

exec "$@"
