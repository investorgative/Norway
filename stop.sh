#!/usr/bin/env bash
# Stops the three NORWAY tmux sessions.
for s in Norway NorwayWeb NorwayCloud; do tmux kill-session -t "$s" 2>/dev/null && echo "stopped $s"; done
exit 0
