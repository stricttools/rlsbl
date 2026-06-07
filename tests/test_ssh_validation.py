"""Tests for SSH host extraction and subtree_remote host validation."""

import subprocess

import pytest

from rlsbl.git_util import extract_ssh_host, validate_subtree_remote_ssh_host


class TestExtractSshHost:
    """Tests for extract_ssh_host parsing."""

    def test_scp_style_short_alias(self):
        assert extract_ssh_host("git@gp:owner/repo.git") == "gp"

    def test_scp_style_full_hostname(self):
        assert extract_ssh_host("git@github.com:owner/repo.git") == "github.com"

    def test_ssh_url_scheme(self):
        assert extract_ssh_host("ssh://git@gp/owner/repo.git") == "gp"

    def test_ssh_url_scheme_full_hostname(self):
        assert extract_ssh_host("ssh://git@github.com/owner/repo.git") == "github.com"

    def test_https_returns_none(self):
        assert extract_ssh_host("https://github.com/owner/repo.git") is None

    def test_empty_string_returns_none(self):
        assert extract_ssh_host("") is None

    def test_none_returns_none(self):
        assert extract_ssh_host(None) is None

    def test_scp_style_with_port_in_host(self):
        """SCP-style URLs don't support ports, but host:port should still parse host."""
        assert extract_ssh_host("git@myhost:owner/repo.git") == "myhost"

    def test_ssh_url_with_port(self):
        """SSH URL with port -- port is part of the host:port, we capture up to /."""
        assert extract_ssh_host("ssh://git@myhost:2222/owner/repo.git") == "myhost:2222"

    def test_http_returns_none(self):
        assert extract_ssh_host("http://github.com/owner/repo.git") is None

    def test_bare_path_returns_none(self):
        assert extract_ssh_host("/path/to/repo.git") is None


class TestValidateSubtreeRemoteSshHost:
    """Tests for validate_subtree_remote_ssh_host."""

    def test_passes_when_hosts_match(self, mock_git_repo):
        """No error when both origin and subtree_remote use the same SSH host."""
        # Set origin to an SSH URL
        subprocess.run(
            ["git", "remote", "add", "origin", "git@gp:owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        # Should not raise or exit
        validate_subtree_remote_ssh_host("git@gp:owner/mirror.git", str(mock_git_repo))

    def test_errors_when_hosts_differ(self, mock_git_repo):
        """Hard error when origin and subtree_remote use different SSH hosts."""
        subprocess.run(
            ["git", "remote", "add", "origin", "git@gp:owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        with pytest.raises(SystemExit) as exc_info:
            validate_subtree_remote_ssh_host(
                "git@github.com:owner/mirror.git", str(mock_git_repo)
            )
        assert exc_info.value.code == 1

    def test_error_message_shows_hosts(self, mock_git_repo, capsys):
        """Error message includes both the mismatched hosts."""
        subprocess.run(
            ["git", "remote", "add", "origin", "git@gp:owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        with pytest.raises(SystemExit):
            validate_subtree_remote_ssh_host(
                "git@github.com:owner/mirror.git", str(mock_git_repo)
            )
        captured = capsys.readouterr()
        assert "github.com" in captured.err
        assert "gp" in captured.err

    def test_skips_when_origin_is_https(self, mock_git_repo):
        """No validation when origin is HTTPS (different auth mechanism)."""
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        # Should not raise even though subtree is SSH
        validate_subtree_remote_ssh_host("git@gp:owner/mirror.git", str(mock_git_repo))

    def test_skips_when_subtree_is_https(self, mock_git_repo):
        """No validation when subtree_remote is HTTPS."""
        subprocess.run(
            ["git", "remote", "add", "origin", "git@gp:owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        validate_subtree_remote_ssh_host(
            "https://github.com/owner/mirror.git", str(mock_git_repo)
        )

    def test_skips_when_both_are_https(self, mock_git_repo):
        """No validation when both are HTTPS."""
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        validate_subtree_remote_ssh_host(
            "https://github.com/owner/mirror.git", str(mock_git_repo)
        )

    def test_skips_when_no_origin(self, mock_git_repo):
        """No validation when origin remote does not exist."""
        # mock_git_repo has no origin by default -- just call directly
        validate_subtree_remote_ssh_host("git@gp:owner/mirror.git", str(mock_git_repo))

    def test_ssh_url_scheme_hosts_match(self, mock_git_repo):
        """Works with ssh:// URL scheme for both origin and subtree_remote."""
        subprocess.run(
            ["git", "remote", "add", "origin", "ssh://git@myhost/owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        validate_subtree_remote_ssh_host(
            "ssh://git@myhost/owner/mirror.git", str(mock_git_repo)
        )

    def test_ssh_url_scheme_hosts_differ(self, mock_git_repo):
        """ssh:// scheme with different hosts errors."""
        subprocess.run(
            ["git", "remote", "add", "origin", "ssh://git@hostA/owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        with pytest.raises(SystemExit) as exc_info:
            validate_subtree_remote_ssh_host(
                "ssh://git@hostB/owner/mirror.git", str(mock_git_repo)
            )
        assert exc_info.value.code == 1

    def test_mixed_scp_and_ssh_url_match(self, mock_git_repo):
        """SCP-style origin and ssh:// subtree with same host passes."""
        subprocess.run(
            ["git", "remote", "add", "origin", "git@myhost:owner/monorepo.git"],
            cwd=str(mock_git_repo),
            check=True,
        )
        validate_subtree_remote_ssh_host(
            "ssh://git@myhost/owner/mirror.git", str(mock_git_repo)
        )
