"""Use-case for health checks."""

from xpst.usecases.base import BaseUseCase, HealthCheckResult, UseCaseDependencies


class HealthCheckUseCase(BaseUseCase):
    """Runs comprehensive health checks on all components."""

    async def execute(self) -> HealthCheckResult:
        """Run health checks on sources, platforms, circuit breakers, state, quotas.

        Returns:
            HealthCheckResult with all health information
        """
        # Check sources
        sources = {}
        for name, source in self.deps.sources.items():
            try:
                sources[name] = await source.check_health()
            except Exception as e:
                sources[name] = {"status": "error", "error": str(e)}
        
        # Check platforms
        platforms = {}
        for name, uploader in self.deps.platforms.items():
            try:
                platforms[name] = await uploader.health_check()
            except Exception as e:
                platforms[name] = {"status": "error", "error": str(e)}
        
        # Check circuit breakers
        circuit_breakers = {}
        for name, cb in self.deps.circuit_breakers.items():
            circuit_breakers[name] = cb.get_status()
        
        # State health
        state = self.deps.state.get_statistics() if hasattr(self.deps.state, 'get_statistics') else {}
        
        # Quota health
        quotas = {}
        if hasattr(self.deps.quota_manager, 'get_all_status'):
            quotas = self.deps.quota_manager.get_all_status()
        
        return HealthCheckResult(
            sources=sources,
            platforms=platforms,
            circuit_breakers=circuit_breakers,
            state=state,
            quotas=quotas
        )