"""strictspec certificate deploy-gate check (tag: preflight).

Consumes a configured strictspec diff certificate as a format_version deploy
gate. Feature-flagged by config presence: skips cleanly when the project has no
``strictspec_gate`` section (no behavior change for the fleet).
"""

from ..errors import ConfigError


def register_strictspec_gate_checks(app):
    """Register the strictspec certificate deploy-gate check on *app*."""

    @app.error_check("strictspec-certificate-gate")
    def check_strictspec_certificate_gate(ctx, reporter):
        """A configured strictspec diff certificate must not report a violated
        (or unsupported-and-unadjudicated) claim."""
        from ..strictspec_gate import CONFIG_KEY, evaluate_certificate_gate

        config = ctx.config
        if config.get(CONFIG_KEY) is None:
            return reporter.skipped("strictspec_gate not configured")

        try:
            verdict = evaluate_certificate_gate(config, str(ctx.project_root))
        except ConfigError as e:
            reporter.error(str(e))
            return reporter.found(str(e).splitlines()[0])

        if verdict.ok:
            if verdict.notes:
                return reporter.passed("; ".join(verdict.notes[:3]))
            return reporter.passed("certificate gate passed")

        for reason in verdict.blocking_reasons:
            reporter.error(reason)
        return reporter.found(
            f"strictspec certificate gate blocks release "
            f"({len(verdict.blocking_reasons)} blocking claim(s))"
        )
