import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout


spec = importlib.util.spec_from_file_location("manage_cache", "manage-cache.py")
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class LocalSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_setup_without_cloud_or_host_registers_before_configuring(self):
        args = SimpleNamespace(snap=True, no_cloud=True, no_host=True)
        with patch.object(cache, "create_container") as create, \
             patch.object(cache, "registered_snap_settings", return_value={"store": "x"}) as register, \
             patch.object(cache, "configure_local") as configure:
            cache.cmd_setup(args)
        create.assert_called_once()
        register.assert_called_once()
        self.assertEqual(configure.call_args.args[2:], ({"store": "x"}, False, False))
        self.assertTrue(cache.load_local_state()["caches"][cache.NAME]["snap_enabled"])

    def test_failed_registration_does_not_configure_or_save_snap_choice(self):
        args = SimpleNamespace(snap=True, no_cloud=False, no_host=False)
        with patch.object(cache, "create_container"), \
             patch.object(cache, "registered_snap_settings", side_effect=SystemExit("failed")), \
             patch.object(cache, "configure_local") as configure:
            with self.assertRaises(SystemExit):
                cache.cmd_setup(args)
        configure.assert_not_called()
        self.assertEqual(cache.load_local_state()["caches"], {})

    def test_registration_runs_only_when_needed(self):
        snap = {"snap-store-proxy": "store"}
        with patch.object(cache, "setup_snap_proxy"), \
             patch.object(cache, "snap_settings", side_effect=[{}, snap]), \
             patch.object(cache, "run") as run:
            self.assertEqual(cache.registered_snap_settings(), snap)
        run.assert_called_once_with("lxc", "exec", cache.NAME, "--",
                                    "snap-store-proxy", "register")

    def test_registration_must_produce_settings(self):
        with patch.object(cache, "setup_snap_proxy"), \
             patch.object(cache, "snap_settings", return_value={}), \
             patch.object(cache, "run"):
            with self.assertRaisesRegex(SystemExit, "usable assertions"):
                cache.registered_snap_settings()

    def test_snap_toggle_updates_only_recorded_destinations(self):
        state = cache.load_local_state()
        state["caches"][cache.NAME] = {
            "snap_enabled": True, "host_configured": True,
            "configured_clouds": {cache.LXD_CLOUD: False},
        }
        cache.save_local_state(state)
        with patch.object(cache, "proxy_url"), \
             patch.object(cache, "configure_local") as configure:
            cache.cmd_snap(SimpleNamespace(snap_action="off"))
        self.assertEqual(configure.call_args.args[2:], ({}, True, False))
        self.assertFalse(cache.load_local_state()["caches"][cache.NAME]["snap_enabled"])

    def test_conflict_preflight_before_either_destination_is_written(self):
        state = cache.load_local_state()
        local = cache.local_cache_state(state)
        with patch.object(cache, "proxy_url", return_value="http://10.0.0.1:3142"), \
             patch.object(cache, "prepare_local_host", return_value=("plan",)), \
             patch.object(cache, "prepare_local_cloud", side_effect=SystemExit("conflict")), \
             patch.object(cache, "apply_local_host") as host:
            with self.assertRaises(SystemExit):
                cache.configure_local(state, local, {}, True, True)
        host.assert_not_called()

    def test_host_preflight_rejects_unmanaged_snap_store(self):
        with patch.object(cache, "host_apt_proxy", return_value=None), \
             patch.object(cache, "strict_host_snap_store", return_value="someone-else"):
            with self.assertRaisesRegex(SystemExit, "unmanaged"):
                cache.prepare_local_host("http://10.0.0.1:3142", {}, {})

    def test_host_removes_previously_managed_snap_store(self):
        state = cache.load_local_state()
        local = cache.local_cache_state(state)
        managed = local.setdefault("host", {"snap store": "old-store"})
        apt = 'Acquire::http::Proxy "http://10.0.0.1:3142";\nAcquire::https::Proxy "DIRECT";\n'
        with patch.object(cache, "run") as run:
            cache.apply_local_host((apt, apt, "", "old-store"), {}, managed, state)
        run.assert_called_once_with("sudo", "snap", "unset", "system", "proxy.store")
        self.assertNotIn("snap store", managed)
        self.assertTrue(cache.load_local_state()["caches"][cache.NAME]["host_configured"])

    def test_cloud_merge_preserves_unrelated_configuration(self):
        existing = {
            cache.LXD_CLOUD: {
                "defined": "local", "type": "lxd",
                "auth-types": ["certificate"], "endpoint": "https://example.invalid",
                "regions": {"local": {"endpoint": "https://region.invalid"}},
                "config": {"project": "custom", "apt-http-proxy": "http://old:3142",
                           "snap-store-proxy": "old-store"},
                "region-config": {"local": {"test-mode": True}},
                "credential-count": 2, "ca-credentials": ["certificate"],
            }
        }
        managed = {"apt-http-proxy": "http://old:3142", "snap-store-proxy": "old-store"}
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout=json.dumps(existing))), \
             patch.object(cache, "no_proxy_list", return_value="localhost"):
            exists, cloud, desired = cache.prepare_local_cloud("http://new:3142", {}, managed)
        self.assertTrue(exists)
        self.assertEqual(cloud["endpoint"], "https://example.invalid")
        self.assertEqual(cloud["regions"], existing[cache.LXD_CLOUD]["regions"])
        self.assertEqual(cloud["region-config"], {"local": {"test-mode": True}})
        self.assertEqual(cloud["ca-certificates"], ["certificate"])
        self.assertEqual(cloud["config"]["project"], "custom")
        self.assertNotIn("snap-store-proxy", cloud["config"])
        self.assertEqual(cloud["config"]["apt-http-proxy"], "http://new:3142")
        self.assertEqual(desired["apt-https-proxy"], "DIRECT")
        self.assertNotIn("credential-count", cloud)

    def test_cloud_rejects_unmanaged_values(self):
        existing = {cache.LXD_CLOUD: {"type": "lxd",
                    "config": {"apt-http-proxy": "http://other:3142"}}}
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout=json.dumps(existing))), \
             patch.object(cache, "no_proxy_list", return_value="localhost"):
            with self.assertRaisesRegex(SystemExit, "unmanaged"):
                cache.prepare_local_cloud("http://new:3142", {}, {})

    def test_cloud_rejects_multiple_regions_to_preserve_default_order(self):
        existing = {cache.LXD_CLOUD: {
            "type": "lxd", "regions": {"first": {}, "second": {}},
        }}
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout=json.dumps(existing))):
            with self.assertRaisesRegex(SystemExit, "multi-region"):
                cache.prepare_local_cloud("http://new:3142", {}, {})

    def test_cloud_rejects_conflicting_region_proxy(self):
        existing = {cache.LXD_CLOUD: {
            "type": "lxd",
            "region-config": {"local": {"apt-http-proxy": "http://other:3142"}},
        }}
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout=json.dumps(existing))), \
             patch.object(cache, "no_proxy_list", return_value="localhost"):
            with self.assertRaisesRegex(SystemExit, "region 'local'"):
                cache.prepare_local_cloud("http://new:3142", {}, {})

    def test_strict_network_discovery_rejects_invalid_response(self):
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout="not json")):
            with self.assertRaisesRegex(SystemExit, "cannot read LXD networks"):
                cache.no_proxy_list(strict=True)

    def test_cloud_write_preserves_definition_and_records_success(self):
        state = cache.load_local_state()
        local = cache.local_cache_state(state)
        managed = local.setdefault("clouds", {}).setdefault(cache.LXD_CLOUD, {})
        cloud = {"type": "lxd", "config": {"project": "custom",
                                            "apt-http-proxy": "http://cache:3142"}}
        desired = {"apt-http-proxy": "http://cache:3142"}
        written = []

        def fake_run(*args, **kwargs):
            with open(args[-1]) as f:
                written.append(json.load(f))
            self.assertEqual(args[:4], ("juju", "update-cloud", cache.LXD_CLOUD, "--client"))

        with patch.object(cache, "run", side_effect=fake_run):
            cache.apply_local_cloud((True, cloud, desired), managed, state)
        self.assertEqual(written, [{"clouds": {cache.LXD_CLOUD: cloud}}])
        saved = cache.load_local_state()["caches"][cache.NAME]
        self.assertEqual(saved["clouds"][cache.LXD_CLOUD], desired)
        self.assertTrue(saved["configured_clouds"][cache.LXD_CLOUD])

    def test_cloud_write_failure_does_not_record_success(self):
        state = cache.load_local_state()
        local = cache.local_cache_state(state)
        managed = local.setdefault("clouds", {}).setdefault(cache.LXD_CLOUD, {})
        with patch.object(cache, "run", side_effect=RuntimeError("update failed")):
            with self.assertRaisesRegex(RuntimeError, "update failed"):
                cache.apply_local_cloud((True, {"type": "lxd"}, {"key": "value"}),
                                        managed, state)
        self.assertEqual(managed, {})
        self.assertNotIn("configured_clouds", local)

    def test_corrupt_state_is_not_silently_reset(self):
        os.makedirs(os.path.dirname(cache.local_state_path()))
        with open(cache.local_state_path(), "w") as f:
            f.write("not json")
        with self.assertRaisesRegex(SystemExit, "cannot read local state"):
            cache.load_local_state()

    def test_host_remove_conflict_preflights_before_removing_either_setting(self):
        state = cache.load_local_state()
        state["caches"][cache.NAME] = {"host": {"apt": "old-apt", "snap store": "old-store"}}
        cache.save_local_state(state)
        with patch.object(cache, "host_apt_proxy", return_value="old-apt"), \
             patch.object(cache, "strict_host_snap_store", return_value="custom-store"), \
             patch.object(cache, "run") as run:
            with self.assertRaisesRegex(SystemExit, "has changed"):
                cache.cmd_host_remove(SimpleNamespace(force=False))
        run.assert_not_called()

    def test_cloud_remove_preserves_unmanaged_config(self):
        existing = {cache.LXD_CLOUD: {"type": "lxd",
                    "config": {"project": "custom", "apt-http-proxy": "http://cache",
                               "snap-store-proxy": "someone-else"}}}
        with patch.object(cache, "run", return_value=SimpleNamespace(stdout=json.dumps(existing))):
            _, cloud, desired = cache.prepare_local_cloud(
                None, {}, {"apt-http-proxy": "http://cache"}, remove=True)
        self.assertEqual(cloud["config"], {"project": "custom",
                                           "snap-store-proxy": "someone-else"})
        self.assertEqual(desired, {})

    def test_cache_stop_requires_explicit_acknowledgement(self):
        with patch.object(cache, "run") as run:
            with self.assertRaisesRegex(SystemExit, "--force"):
                cache.cmd_cache_lifecycle(SimpleNamespace(action="stop", force=False))
        run.assert_not_called()

    def test_charmcraft_env_missing_lxc_is_safe_to_eval(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cache, "cache_ip", side_effect=FileNotFoundError("lxc")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            cache.cmd_charmcraft_env(SimpleNamespace(unset=False))
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("not using a cache", stderr.getvalue())


class JujuCommandsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def args(**overrides):
        values = {"action": "apply", "controller": "test", "defaults_only": False,
                  "exclude_model": [], "force": False}
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def model_config(proxy=""):
        return {key: {"Value": proxy if key == "apt-http-proxy" else "",
                      "Source": "model"} for key in cache.PROXY_KEYS}

    def juju_responses(self, second_proxy=""):
        models = [
            {"name": "admin/one", "short-name": "one", "model-uuid": "model-one"},
            {"name": "other/two", "short-name": "two", "model-uuid": "model-two"},
        ]

        def respond(*args, **kwargs):
            if args[0] == "clouds":
                return {"local": {"type": "lxd"}}
            if args[0] == "models":
                return {"models": models}
            if args[0] == "model-defaults":
                return {key: {} for key in cache.PROXY_KEYS}
            if args[0] == "model-config":
                if args[2] == "test:model-one":
                    return self.model_config()
                if args[2] == "test:model-two":
                    return self.model_config(second_proxy)
            raise AssertionError(args)

        return respond

    def test_controller_preflights_every_model_before_writing(self):
        with patch.object(cache, "resolve_controller", return_value=("test", "controller-id")), \
             patch.object(cache, "proxy_settings", return_value={"apt-http-proxy": "http://cache"}), \
             patch.object(cache, "juju_json", side_effect=self.juju_responses("http://custom")) as read, \
             patch.object(cache, "run") as write:
            with self.assertRaisesRegex(SystemExit, "preflight found conflicts"):
                cache.cmd_controller(self.args())
        self.assertTrue(any(c.args[:3] == ("model-config", "-m", "test:model-one")
                            for c in read.call_args_list))
        self.assertTrue(any(c.args[:3] == ("model-config", "-m", "test:model-two")
                            for c in read.call_args_list))
        write.assert_not_called()

    def test_controller_command_with_real_juju_json_shapes(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if "--format" not in args:
                return SimpleNamespace(returncode=0)
            command = args[1]
            if command == "controllers":
                data = {"current-controller": "test",
                        "controllers": {"test": {"uuid": "controller-id"}}}
            elif command == "clouds":
                data = {"local": {"type": "lxd"}}
            elif command == "model-defaults":
                data = {"apt-http-proxy": {"default": ""}}
            elif command == "models":
                data = {"models": [{"name": "admin/one", "short-name": "one",
                                     "model-uuid": "model-one"}]}
            elif command == "model-config":
                data = self.model_config()
            else:
                raise AssertionError(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        with patch.dict(os.environ, {"JUJU_CONTROLLER": ""}), \
             patch.object(cache, "proxy_settings", return_value={"apt-http-proxy": "http://cache"}), \
             patch.object(cache, "run", side_effect=fake_run):
            cache.cmd_controller(self.args(controller=None))
        self.assertIn(("juju", "models", "-c", "test", "--all", "--format", "json"), calls)
        self.assertIn(("juju", "model-config", "-m", "test:model-one",
                       "apt-http-proxy=http://cache"), calls)

    def test_exclusion_applies_defaults_and_remaining_models(self):
        with patch.object(cache, "resolve_controller", return_value=("test", "controller-id")), \
             patch.object(cache, "proxy_settings", return_value={"apt-http-proxy": "http://cache"}), \
             patch.object(cache, "juju_json", side_effect=self.juju_responses("http://custom")), \
             patch.object(cache, "run") as write:
            cache.cmd_controller(self.args(exclude_model=["two"]))
        calls = [call.args for call in write.call_args_list]
        self.assertIn(("juju", "model-defaults", "-c", "test", "--cloud", "local",
                       "apt-http-proxy=http://cache"), calls)
        self.assertIn(("juju", "model-config", "-m", "test:model-one",
                       "apt-http-proxy=http://cache"), calls)
        self.assertFalse(any("test:model-two" in call for call in calls))
        saved = cache.load_local_state()["caches"][cache.NAME]["juju_targets"]
        self.assertEqual(saved["model:model-one"]["apt-http-proxy"], "http://cache")

    def test_force_overrides_conflict(self):
        with patch.object(cache, "resolve_controller", return_value=("test", "controller-id")), \
             patch.object(cache, "proxy_settings", return_value={"apt-http-proxy": "http://cache"}), \
             patch.object(cache, "juju_json", side_effect=self.juju_responses("http://custom")), \
             patch.object(cache, "run") as write:
            cache.cmd_controller(self.args(force=True))
        self.assertIn(("juju", "model-config", "-m", "test:model-two",
                       "apt-http-proxy=http://cache"),
                      [call.args for call in write.call_args_list])

    def test_inaccessible_model_blocks_all_changes(self):
        responses = self.juju_responses()

        def respond(*args, **kwargs):
            if args[:3] == ("model-config", "-m", "test:model-two"):
                raise subprocess.CalledProcessError(1, args)
            return responses(*args, **kwargs)

        with patch.object(cache, "resolve_controller", return_value=("test", "controller-id")), \
             patch.object(cache, "proxy_settings", return_value={"apt-http-proxy": "http://cache"}), \
             patch.object(cache, "juju_json", side_effect=respond), \
             patch.object(cache, "run") as write:
            with self.assertRaisesRegex(SystemExit, "preflight failed"):
                cache.cmd_controller(self.args())
        write.assert_not_called()

    def test_model_listing_stderr_blocks_partial_listing(self):
        result = SimpleNamespace(returncode=0, stdout='{"models":[]}', stderr="model error\n")
        with patch.object(cache, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "model error"):
                cache.controller_models("test")

    def test_exclusions_must_be_unambiguous_and_exist(self):
        models = [
            {"name": "alice/shared", "short-name": "shared", "model-uuid": "uuid-1"},
            {"name": "bob/shared", "short-name": "shared", "model-uuid": "uuid-2"},
        ]
        with self.assertRaisesRegex(ValueError, "matches 2"):
            cache.select_models(models, ["shared"])
        with self.assertRaisesRegex(ValueError, "matches 0"):
            cache.select_models(models, ["missing"])
        self.assertEqual(cache.select_models(models, ["alice/shared"]), [models[1]])

    def test_regional_override_is_preflighted(self):
        data = {"apt-http-proxy": {"regions": [{"name": "region", "value": "http://custom"}]}}
        with patch.object(cache, "juju_json", return_value=data):
            targets = cache.defaults_targets("test", "controller-id", "local")
        with self.assertRaisesRegex(ValueError, "preflight found conflicts"):
            cache.plan_juju_changes(targets, {}, {"apt-http-proxy": "http://cache"}, False, False)

    def test_remove_resets_only_recorded_values(self):
        target = cache.JujuTarget("model:id", "model test:one", ["model-config", "-m", "test:id"],
                                  {"apt-http-proxy": ("http://cache", "model"),
                                   "apt-no-proxy": ("custom", "model")})
        managed = {"model:id": {"apt-http-proxy": "http://cache"}}
        plans = cache.plan_juju_changes([target], managed, {}, True, False)
        self.assertEqual(plans[0][1], [("apt-http-proxy", None)])

    def test_remove_refuses_changed_managed_value(self):
        target = cache.JujuTarget("model:id", "model test:one", [],
                                  {"apt-http-proxy": ("http://custom", "model")})
        managed = {"model:id": {"apt-http-proxy": "http://cache"}}
        with self.assertRaisesRegex(ValueError, "preflight found conflicts"):
            cache.plan_juju_changes([target], managed, {}, True, False)

    def test_snap_off_explicitly_clears_an_inherited_managed_proxy(self):
        target = cache.JujuTarget("model:id", "model test:one", [],
                                  {"snap-store-proxy": ("store-id", "controller")})
        managed = {"model:id": {"snap-store-proxy": "store-id"}}
        plans = cache.plan_juju_changes([target], managed, {}, False, False)
        self.assertEqual(plans[0][1], [("snap-store-proxy", "")])

    def test_single_model_remove_resolves_uuid_and_resets_managed_keys(self):
        state = cache.load_local_state()
        state["caches"][cache.NAME] = {
            "juju_targets": {"model:uuid": {"apt-http-proxy": "http://cache"}}
        }
        cache.save_local_state(state)

        def respond(*args, **kwargs):
            if args[0] == "show-model":
                return {"mymodel": {"model-uuid": "uuid"}}
            if args[0] == "model-config":
                return self.model_config("http://cache")
            raise AssertionError(args)

        with patch.object(cache, "resolve_controller", return_value=("test", "controller-id")), \
             patch.object(cache, "juju_json", side_effect=respond), \
             patch.object(cache, "run") as run:
            cache.cmd_model(SimpleNamespace(action="remove", model="test:mymodel", force=False))
        run.assert_called_once_with("juju", "model-config", "-m", "test:uuid",
                                    "--reset", "apt-http-proxy")
        self.assertNotIn("model:uuid",
                         cache.load_local_state()["caches"][cache.NAME]["juju_targets"])

    def test_partial_failure_records_only_successful_keys(self):
        state = cache.load_local_state()
        local = cache.local_cache_state(state)
        managed = local.setdefault("juju_targets", {})
        target = cache.JujuTarget("model:id", "model test:one",
                                  ["model-config", "-m", "test:id"], {})
        plans = [(target, [("apt-http-proxy", "http://cache"), ("apt-no-proxy", "localhost")])]
        with patch.object(cache, "run", side_effect=[None, subprocess.CalledProcessError(1, ["juju"])]):
            with self.assertRaisesRegex(SystemExit, "incomplete"):
                cache.execute_juju_changes(plans, managed, state)
        saved = cache.load_local_state()["caches"][cache.NAME]["juju_targets"]
        self.assertEqual(saved["model:id"], {"apt-http-proxy": "http://cache"})

    def test_parser_uses_grouped_names_and_rejects_legacy(self):
        parser = cache.build_parser()
        self.assertEqual(parser.parse_args(["controller", "apply"]).action, "apply")
        self.assertEqual(parser.parse_args(["model", "remove", "test:model"]).action, "remove")
        self.assertEqual(parser.parse_args(["snap", "off"]).snap_action, "off")
        self.assertEqual(parser.parse_args(["charmcraft", "env"]).func, cache.cmd_charmcraft_env)
        with self.assertRaises(SystemExit):
            parser.parse_args(["enable"])


if __name__ == "__main__":
    unittest.main()
