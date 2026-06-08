#!/bin/bash
# xPST Docker Entrypoint
set -e

# If the first argument is a xpst subcommand, run it
case "$1" in
    run|watch|post|health|status|setup|update|version|auth|backfill|logs|dashboard|delete)
        exec xpst "$@"
        ;;
    *)
        # Default: run xpst with all arguments
        exec xpst "$@"
        ;;
esac
