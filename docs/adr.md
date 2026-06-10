# Architecture Decision Records (ADRs)

## ADR-001: Use-Case Layer with Dependency Injection

**Status**: Accepted
**Date**: 2026-06-09

### Context
The original engine had business logic mixed with infrastructure concerns (I/O, API calls, state management). This made testing difficult and prevented parallel execution.

### Decision
Extract business logic into pure use-case classes that receive dependencies via constructor (Dependency Injection).

### Consequences
- ✅ Testable with mocks
- ✅ Parallel execution possible
- ✅ Clean separation of concerns
- ✅ Single Responsibility Principle
- ⚠️ More files/classes
- ⚠️ Learning curve for new contributors

---

## ADR-002: StateManager Split (Store vs Manager)

**Status**: Accepted
**Date**: 2026-06-09

### Context
The original StateManager combined:
- Atomic file I/O with locking
- Corruption recovery and backup rotation
- Business logic for video tracking, deduplication, health metrics

### Decision
Split into:
- `StateStore`: Pure I/O, locking, corruption recovery
- `StateManager`: Business logic (video tracking, DLQ, statistics)

Legacy `StateManager` API preserved via compatibility wrapper.

### Consequences
- ✅ Single Responsibility
- ✅ Swappable storage backends
- ✅ Easier testing (mock store)
- ✅ Backward compatible
- ⚠️ More classes

---

## ADR-003: SessionManager as Single Auth Source

**Status**: Accepted
**Date**: 2026-06-09

### Context
Each platform uploader and source had duplicate credential loading logic (~244 lines total). This caused:
- Inconsistent behavior
- Multiple auth code paths
- Difficult testing

### Decision
Centralize all authentication in `SessionManager`:
- Platform uploaders delegate to `SessionManager.get_*_client()`
- Sources delegate to `SessionManager.get_*_client()`
- Fallback direct methods for testing

### Consequences
- ✅ Single source of truth for auth
- ✅ Consistent error handling
- ✅ Easier to add new auth methods
- ✅ Removed ~200 lines of duplicate code

---

## ADR-004: Encrypted Credential Fallback

**Status**: Accepted
**Date**: 2026-06-09

### Context
When OS keychain unavailable, credentials were stored in plain JSON files.

### Decision
Use Fernet encryption with argon2id key derivation for file-based fallback:
- Per-file encryption keys derived from master key
- Master key derived from `CredentialStore` instance ID
- `.enc` extension for encrypted files

### Consequences
- ✅ Defense in depth
- ✅ Works on headless servers
- ⚠️ Key management complexity
- ⚠️ Slightly slower first access

---

## ADR-005: CLI Agent-Readiness

**Status**: Accepted
**Date**: 2026-06-09

### Context
CLI was designed for human use only. Needed to support AI agents and automation.

### Decision
All commands support:
- `--json` flag for machine output
- `--quiet` to suppress decorative output
- `--dry-run` for safe preview
- Auto-JSON when stdout is not a TTY
- Meaningful exit codes (0, 1, 2, 3, 4, 10)

### Consequences
- ✅ AI agent compatible
- ✅ CI/CD pipeline friendly
- ✅ Scripting friendly
- ✅ Unix philosophy compliance

---

## ADR-006: MCP Server for AI Integration

**Status**: Accepted
**Date**: 2026-06-09

### Context
Need to expose xPST functionality to AI assistants (Claude, Cursor, etc.).

### Decision
Implement stdio-based MCP server with tools for:
- Video fetching and cross-posting
- Health checks and status
- Configuration and auth management

### Consequences
- ✅ Native AI integration
- ✅ Standard protocol (MCP)
- ✅ Discoverable capabilities
- ⚠️ Requires mcp package

---

## ADR-007: Prometheus Metrics

**Status**: Accepted
**Date**: 2026-06-09

### Context
Need observability for production deployments.

### Decision
Optional Prometheus metrics via `prometheus_client`:
- `xpst_uploads_total` (counter)
- `xpst_upload_duration_seconds` (histogram)
- `xpst_encoding_duration_seconds` (histogram)
- `xpst_active_platforms` (gauge)
- `xpst_circuit_breaker_state` (gauge)
- Exposed at `/metrics` on dashboard

### Consequences
- ✅ Industry standard
- ✅ Zero cost if not installed
- ✅ Works with Grafana/Alertmanager
- ⚠️ Optional dependency

---

## ADR-008: Config Migration System

**Status**: Accepted
**Date**: 2026-06-09

### Context
Configuration schema evolves over time. Need automatic migration.

### Decision
Versioned config migrations:
- Config has `version` field
- Migrations: v1→v2→v3→v4
- Automatic on config load
- Timestamped backups in `~/.xpst/backups/`

### Consequences
- ✅ Zero-downtime upgrades
- ✅ Rollback via backups
- ✅ Clear audit trail
- ⚠️ Migration logic must be idempotent

---

## ADR-009: Circuit Breaker Pattern

**Status**: Accepted
**Date**: 2026-06-09

### Context
Platform APIs fail intermittently. Need to prevent cascade failures.

### Decision
Per-platform circuit breaker with 3 states:
- **Closed**: Normal operation, count failures
- **Open**: Short-circuit, fail fast (5 min timeout)
- **Half-open**: Test with limited requests

Persisted in state file for cross-process consistency.

### Consequences
- ✅ Automatic failure isolation
- ✅ Self-healing
- ✅ Observability via metrics
- ⚠️ May delay recovery

---

## ADR-010: Anti-Bot Human-Like Behavior

**Status**: Accepted
**Date**: 2026-06-09

### Context
Social platforms detect and block automated behavior.

### Decision
Configurable delays with jitter:
- Random delay between requests (configurable min/max)
- Per-platform user agents
- Browser cookie extraction for auth
- Wake check to detect sleep/hibernate

### Consequences
- ✅ Reduces false positives
- ✅ Configurable per environment
- ⚠️ Slower throughput
- ⚠️ Not a guarantee