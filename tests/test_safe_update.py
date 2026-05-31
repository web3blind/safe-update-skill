import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'safe_update.py'
spec = importlib.util.spec_from_file_location('safe_update', MODULE_PATH)
safe_update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe_update)


class SafeUpdateTests(unittest.TestCase):
    def test_extract_ready_plugin_list_reads_telegram_ready_line(self):
        text = '2026-04-25T04:27:08.296+00:00 [gateway] ready (3 plugins: browser, telegram, telegram-read; 17.1s)'
        plugins = safe_update.extract_ready_plugin_list(text)
        self.assertEqual([['browser', 'telegram', 'telegram-read']], plugins)

    def test_analyze_gateway_journal_detects_telegram_ready_and_cache_errors(self):
        text = '\n'.join([
            "Apr 25 04:26:32 node[1]: [plugins] telegram failed to load from /x: Error: EACCES: permission denied, open '/tmp/cache/plugin.cjs'",
            'Apr 25 04:27:08 node[2]: [gateway] ready (3 plugins: browser, telegram, telegram-read; 17.1s)',
        ])
        analysis = safe_update.analyze_gateway_journal(text)
        self.assertTrue(analysis['ready_with_telegram'])
        self.assertEqual(1, len(analysis['cache_permission_lines']))
        self.assertEqual(1, len(analysis['error_lines']))

    def test_should_attempt_cache_autofix_only_when_telegram_not_ready(self):
        self.assertTrue(safe_update.should_attempt_cache_autofix({
            'ready_with_telegram': False,
            'cache_permission_lines': ['x'],
        }))
        self.assertFalse(safe_update.should_attempt_cache_autofix({
            'ready_with_telegram': True,
            'cache_permission_lines': ['x'],
        }))

    def test_upsert_systemd_service_env_adds_env_inside_service_section(self):
        original = """[Unit]\nDescription=Gateway\n\n[Service]\nExecStart=/usr/bin/node app.js\n\n[Install]\nWantedBy=default.target\n"""
        updated, changed = safe_update.upsert_systemd_service_env(original, 'CACHE_FS_CACHE', '/home/assistent/.hermes/tmp/safe-update-cache')
        self.assertTrue(changed)
        self.assertIn("Environment=CACHE_FS_CACHE=/home/assistent/.hermes/tmp/safe-update-cache", updated)
        self.assertLess(updated.index('[Service]'), updated.index('Environment=CACHE_FS_CACHE=/home/assistent/.hermes/tmp/safe-update-cache'))
        self.assertLess(updated.index('Environment=CACHE_FS_CACHE=/home/assistent/.hermes/tmp/safe-update-cache'), updated.index('[Install]'))

    def test_upsert_systemd_service_env_replaces_existing_value(self):
        original = """[Service]\nEnvironment=CACHE_FS_CACHE=/tmp/cache\nExecStart=/usr/bin/node app.js\n"""
        updated, changed = safe_update.upsert_systemd_service_env(original, 'CACHE_FS_CACHE', '/home/assistent/.hermes/tmp/safe-update-cache')
        self.assertTrue(changed)
        self.assertIn("Environment=CACHE_FS_CACHE=/home/assistent/.hermes/tmp/safe-update-cache", updated)
        self.assertNotIn("Environment=CACHE_FS_CACHE=/tmp/cache", updated)

    def test_build_gateway_plugin_checks_fails_when_telegram_not_ready_and_cache_present(self):
        journal_result = {
            'ok': True,
            'out': 'telegram failed ... EACCES /tmp/cache/x.cjs',
            'analysis': {
                'ready_with_telegram': False,
                'error_lines': ['telegram failed ... EACCES /tmp/cache/x.cjs'],
                'cache_permission_lines': ['telegram failed ... EACCES /tmp/cache/x.cjs'],
            },
            'since': '2026-04-25 04:20:00 UTC',
        }
        checks = safe_update.build_gateway_plugin_checks(journal_result)
        self.assertFalse(checks['gateway_ready_with_telegram']['ok'])
        self.assertFalse(checks['gateway_no_cache_permission_errors']['ok'])
        self.assertFalse(checks['gateway_no_telegram_plugin_failures']['ok'])

    def test_render_report_includes_gateway_restart_autofix_and_telegram_ready_summary(self):
        state = {
            'updatedAt': '2026-04-25T04:44:00+00:00',
            'backupPath': '/tmp/backup.tar.gz',
            'updateExecuted': True,
            'updateSkipped': False,
            'gatewayRestart': {'ok': True, 'reason': 'post-update activation', 'at': '2026-04-25T04:44:01+00:00'},
            'postcheck': {
                'gateway_ready_with_telegram': {'ok': True},
                'gateway_cache_autofix': {'ok': True, 'skipped': False},
                'postcheck_summary': {'ok': True, 'failed_checks': []},
            },
            'notes': [],
        }
        safe_update.render_report(state)
        report = safe_update.REPORT_FILE.read_text(encoding='utf-8')
        self.assertIn('- Gateway restart: ok', report)
        self.assertIn('- Telegram ready: True', report)
        self.assertIn('- Cache auto-fix: ok', report)
        self.assertIn('## Gateway activation', report)
        self.assertIn('## Telegram plugin readiness', report)

    def test_detect_install_health_flags_missing_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entrypoint = Path(tmpdir) / 'dist' / 'index.js'
            results = {
                'hermes_version': {'ok': True, 'out': '1.2.3'},
                'entrypoint_present': safe_update.check_entrypoint_present(entrypoint),
            }
            health = safe_update.summarize_install_health(results)
            self.assertFalse(health['ok'])
            self.assertIn('entrypoint_present', health['failed_checks'])

    def test_detect_install_health_passes_when_version_and_entrypoint_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entrypoint = Path(tmpdir) / 'dist' / 'index.js'
            entrypoint.parent.mkdir(parents=True, exist_ok=True)
            entrypoint.write_text('// ok', encoding='utf-8')
            results = {
                'hermes_version': {'ok': True, 'out': '1.2.3'},
                'entrypoint_present': safe_update.check_entrypoint_present(entrypoint),
            }
            health = safe_update.summarize_install_health(results)
            self.assertTrue(health['ok'])
            self.assertEqual([], health['failed_checks'])

    def test_should_skip_recent_successful_update_inside_cooldown(self):
        state = {'lastSuccessfulUpdateAt': '2026-04-11T12:00:00+00:00'}
        decision = safe_update.should_skip_due_to_recent_success(state, now_ts='2026-04-11T12:10:00+00:00', cooldown_seconds=900)
        self.assertTrue(decision['skip'])
        self.assertGreater(decision['remaining_seconds'], 0)

    def test_should_not_skip_when_outside_cooldown(self):
        state = {'lastSuccessfulUpdateAt': '2026-04-11T12:00:00+00:00'}
        decision = safe_update.should_skip_due_to_recent_success(state, now_ts='2026-04-11T12:20:00+00:00', cooldown_seconds=900)
        self.assertFalse(decision['skip'])
        self.assertEqual(0, decision['remaining_seconds'])

    def test_preflight_cache_health_fails_when_npm_verify_fails(self):
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_run(cmd):
                calls.append(cmd)
                if cmd == 'npm config get cache':
                    return 0, tmpdir, ''
                if cmd == f'npm cache verify --cache {tmpdir}':
                    return 1, '', 'ENOENT'
                raise AssertionError(cmd)

            health = safe_update.check_npm_cache_health(fake_run)
            self.assertFalse(health['ok'])
            self.assertEqual(tmpdir, health['path'])
            self.assertIn(f'npm cache verify --cache {tmpdir}', calls)

    def test_postcheck_requires_verified_install_even_if_update_exited_zero(self):
        checks = {
            'hermes_status_after': {'ok': True},
            'gateway_status_after': {'ok': True},
            'heartbeat_state_present': {'ok': True},
            'hermes_version_after': {'ok': False},
            'entrypoint_present_after': {'ok': False},
        }
        summary = safe_update.summarize_postcheck(checks)
        self.assertFalse(summary['ok'])
        self.assertIn('hermes_version_after', summary['failed_checks'])
        self.assertIn('entrypoint_present_after', summary['failed_checks'])

    def test_validate_update_command_blocks_unprivileged_custom_command_for_system_install(self):
        result = safe_update.validate_update_command_for_install(
            'custom-hermes-update --no-privilege',
            system_install=True,
        )
        self.assertFalse(result['ok'])
        self.assertIn('SAFE_UPDATE_UPDATE_CMD', result['reason'])
        self.assertIn('~/.hermes/hermes-agent', result['reason'])

    def test_validate_update_command_allows_default_sudo_command_for_system_install(self):
        result = safe_update.validate_update_command_for_install(
            safe_update.DEFAULT_UPDATE_CMD,
            system_install=True,
        )
        self.assertTrue(result['ok'])

    def test_validate_update_command_allows_unprivileged_command_for_non_system_install(self):
        result = safe_update.validate_update_command_for_install(
            'custom-hermes-update --no-privilege',
            system_install=False,
        )
        self.assertTrue(result['ok'])


if __name__ == '__main__':
    unittest.main()
