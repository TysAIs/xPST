#!/bin/bash
# xPST Docker Entrypoint
set -e

# If the first argument is a xpst subcommand, run it
case "$1" in
    analytics|app|auth|backfill|build|config|connect|dashboard|delete|diagnostics|health|logs|mcp|plugins|post|providers|readiness|run|schedule|setup|status|update|version|watch)
        exec xpst "$@"
        ;;
    *)
        # Default: run xpst with all arguments
        exec xpst "$@"
        ;;
esac
