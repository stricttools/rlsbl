"""strictspec certificate deploy-gate check (tag: preflight).

Consumes a configured strictspec diff certificate as a format_version deploy
gate. The check is the option ``rlsbl:strictspec-certificate-gate``, off by
default: a project adopts it by switching the option on, and the
``strictspec_gate`` section it then requires names the certificate.
"""

from ..errors import ConfigError
from ._common import exception_text, summary_line


def register_strictspec_gate_checks(app):
    """Register the strictspec certificate deploy-gate check on *app*."""

    @app.error_check("strictspec-certificate-gate")
    def check_strictspec_certificate_gate(ctx, reporter):
        """A configured strictspec diff certificate must not report a violated
        (or unsupported-and-unadjudicated) claim."""
        from ..strictspec_gate import CONFIG_KEY, evaluate_certificate_gate

        config = ctx.config
        if config.get(CONFIG_KEY) is None:
            # The check runs only while its option is on, so the section the
            # option requires is missing: the disagreement config-schema
            # also reports.
            msg = (
                f"rlsbl:strictspec-certificate-gate is on, but "
                f".rlsbl/config.json declares no {CONFIG_KEY}. Declare "
                f'"{CONFIG_KEY}": {{"certificate": "<path>"}}, or switch the '
                f"option off by deleting its entry."
            )
            reporter.error(msg)
            return reporter.found(msg)

        try:
            verdict = evaluate_certificate_gate(config, str(ctx.project_root))
        except ConfigError as e:
            # A message-less exception stringifies to "", which the reporter
            # rejects -- twice over here, since the outcome also indexes the
            # first line of it.
            text = exception_text(e)
            reporter.error(text)
            return reporter.found(summary_line(text))

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
