"""The one place vcctrl parses configuration.

Imported by vcctrld on the daemon host and by the tools in bin/ on the control
host, so it must stay importable from both and must not drag in evdev, Pillow
or anything else that only exists on the Pi.

WHY THIS MODULE REFUSES TO OFFER dict.get()
===========================================

A YAML mapping has three states a caller can be in, and almost every consumer
collapses them:

    absent          the key is not in the file at all
    explicit null   the key is present and set to nothing, deliberately
    a value         including falsy ones -- 0, "", False

`cfg.get("x", False)` flattens all three into one answer, which is
`st.get("usb4vc", {})` wearing a nicer hat. That idiom printed

    input devices held by USB4VC: ok

off a field that was not in the payload at all, immediately before a 23-minute
unattended run. The same shape is why a `leds` reading omits its value keys
instead of publishing zeroes, and why `board` reports `unknown` rather than
defaulting to the IBM PC board.

So there is no `get()` here. There are three accessors and the caller has to
say which of the three states it is prepared to handle:

    require(path)          present and non-null, or ConfigError
    optional(path)         returns ABSENT / NONE / the value -- caller branches
    default(path, x)       x when ABSENT; ConfigError on an explicit null,
                           because "deliberately nothing" and "unspecified"
                           are different instructions and silently substituting
                           a default for the first one ignores what was written

`default()` raising on explicit null is the load-bearing part. It is the only
accessor a hurried caller reaches for, so it is the one that must not be able
to flatten the distinction.

PRECEDENCE
==========

    built-in defaults  <  config file  <  VCCTRL_* env  <  CLI flag

Env stays above the file deliberately: the systemd drop-ins already use it, CI
wants it, and `VCCTRL_HOST=other-pi vcctrl status` is worth keeping. CLI flags
are the caller's business and are applied above this module.

VCCTRL_FORCE is NOT in the schema and never will be. It is not configuration,
it is a deliberate override of the deploy guard that refuses while the input
lock is held. Awkward to reach for is its entire function, and a config key is
not awkward.

READ ONCE
=========

load() is called once at start and the result is immutable thereafter. There is
no live reload. A configuration that can change under a running measurement
cell is a shared mutable value with no owner, which this project has already
paid for once.
"""

import os
import sys

__all__ = ["ABSENT", "NONE", "Config", "ConfigError", "load", "find_config_file"]


class ConfigError(Exception):
    """Raised for a malformed file, an unknown key, or a misused accessor.

    Callers in the daemon catch this at start-up and record the capability as
    failed rather than dying -- see Rule 2 in vcctrld. Callers in bin/ print it
    and exit, because a control-side tool with no configuration has nothing
    useful to do.
    """


class _Sentinel(object):
    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name

    # Deliberately NOT falsy. `if cfg.optional("x"):` must not quietly mean
    # "absent", because that is the collapse this module exists to prevent --
    # a caller that wants a boolean has to compare against the sentinel.
    def __bool__(self):
        raise ConfigError(
            "%s has no truth value -- compare against vcconfig.%s explicitly. "
            "An absent key and a false value are different answers." % (
                self._name, self._name))

    __nonzero__ = __bool__          # py2 name, harmless here


ABSENT = _Sentinel("ABSENT")
NONE = _Sentinel("NONE")


# ---------------------------------------------------------------------------
# Schema.
#
# Unknown keys are an ERROR, not a shrug. A typo'd `kasa_hostt` that silently
# leaves the plug unconfigured is worse than a refusal to start: the refusal
# names the typo, and the shrug produces a rig that looks configured and has no
# power control.
#
# Vocabulary:
#   a type            a scalar of that type (bool before int: bool IS an int)
#   (t1, t2)          any of these types
#   {...}             a mapping whose keys must all appear here
#   ("list", schema)  a list whose every item matches schema
#   ("map", schema)   a mapping with caller-chosen keys, values matching schema
#   ANY               unvalidated -- used only where the shape is genuinely
#                     open, and every use is a known gap rather than a shortcut
# ---------------------------------------------------------------------------

ANY = ("any",)

_ADDR = (str,)
_PORT = (int,)

SCHEMA = {
    "version": int,

    "rig": {
        "name": str,
    },

    # The control host: the machine bin/vcctrl is run from.
    "control": {
        "daemon_host": str,          # ssh destination
        "web": str,                  # base URL of the daemon's web UI
        "shots_dir": str,
        "ssh_control_dir": str,
        "fileserver": {
            "kind": str,             # ftp | none
            "host": str,
            "port": int,
            "user": str,
            # NEVER a literal password. This names an environment variable.
            "password_env": str,
        },
    },

    # The daemon host: paths and listeners on the machine running vcctrld.
    "daemon": {
        "prefix": str,
        "state_dir": str,
        "socket": str,
        "usb4vc": {
            "app_dir": str,
            "debug_log": str,
            "board_file": str,
        },
        "web": {
            "bind": str,
            "port": int,
            "tls_port": int,
            "tls": {
                "provider": str,     # tailscale | files | none
                "cert": str,
                "key": str,
            },
        },
    },

    "capabilities": ("map", {
        "backend": str,
        # Per-backend settings cannot be validated until the backends exist and
        # can declare their own shapes. KNOWN GAP, closed in phase 4 of
        # docs/CONFIG-PLAN.md -- until then a typo inside `settings` is not
        # caught here, and that is stated rather than hidden.
        "settings": ANY,
    }),

    "targets": ("list", {
        "board_id": int,
        "name": str,
        "native": {"width": int, "height": int},
        # A WORD, not a boolean. `false` collapses "this board has no such
        # channel" into "the channel is broken", and the daemon distinguishes
        # those. See LedsCapability.support().
        "leds": str,                 # supported | unsupported | unknown
    }),

    "harness": {
        "profile": str,
        "timing": {
            # Rig physics: BIOS keyboard buffer depth. NOT the menu window,
            # which is target physics and belongs in the profile, and NOT
            # menu_max_keys, which is DERIVED from both and is computed and
            # asserted rather than configured. A derived value presented as
            # configuration is a value with its reasoning deleted.
            "kbd_buffer_keys": int,
        },
    },
}


# Built-in defaults. Every one of these is what the code did before this module
# existed, so a rig with no config file behaves exactly as it did.
DEFAULTS = {
    "daemon": {
        "prefix": "/opt/vcctrl",
        "state_dir": "/var/lib/vcctrl",
        "socket": "/run/vcctrl.sock",
        "usb4vc": {
            "app_dir": "/home/pi/usb4vc/rpi_app",
            "debug_log": "/home/pi/usb4vc/usb4vc_debug_log.txt",
            "board_file": "/run/usb4vc/board.json",
        },
        "web": {
            "bind": "127.0.0.1",
            "port": 8080,
            "tls_port": 8443,
            "tls": {
                "provider": "tailscale",
                "cert": "/var/lib/vcctrl/tls.crt",
                "key": "/var/lib/vcctrl/tls.key",
            },
        },
    },
    "control": {
        "daemon_host": "usb4vc",
        "shots_dir": "/tmp/vcctrl-shots",
    },
    "harness": {
        "timing": {"kbd_buffer_keys": 15},
    },
}


# Environment overrides, applied ABOVE the file. Keyed by the dotted path they
# override. VCCTRL_FORCE is absent by design -- see the module docstring.
ENV_OVERRIDES = {
    "VCCTRL_HOST": "control.daemon_host",
    "VCCTRL_WEB": "control.web",
    "VCCTRL_PI": "control.daemon_host",
    "VCCTRL_SHOTS": "control.shots_dir",
    "VCCTRL_SSH_CTL_DIR": "control.ssh_control_dir",
    "VCCTRL_WEB_BIND": "daemon.web.bind",
    "VCCTRL_WEB_PORT": "daemon.web.port",
    "VCCTRL_WEB_TLS_PORT": "daemon.web.tls_port",
    "VCCTRL_ALSA": "capabilities.audio.settings.device",
    "VCCTRL_VIDEO": "capabilities.video.settings.device",
}

# Paths whose env override must be coerced out of the string the shell gives.
_INT_PATHS = frozenset([
    "daemon.web.port", "daemon.web.tls_port",
])


def _merge(base, over):
    """Deep-merge `over` onto a copy of `base`. Mappings recurse; anything else
    replaces wholesale -- a list in the file replaces the default list rather
    than appending to it, because a partially-overridden list is a shape nobody
    can reason about."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _typename(t):
    if isinstance(t, tuple):
        return "/".join(getattr(x, "__name__", str(x)) for x in t)
    return getattr(t, "__name__", str(t))


def _validate(node, schema, path, errors):
    """Walk the tree against the schema, collecting EVERY problem.

    Collecting rather than raising on the first one is deliberate: a config
    with four typos should report four typos, not force four edit-run cycles.
    """
    if schema is ANY:
        return

    if isinstance(schema, dict):
        if not isinstance(node, dict):
            errors.append("%s: expected a mapping, got %s"
                          % (path or "(root)", type(node).__name__))
            return
        for k, v in node.items():
            # A key beginning with _ is a comment carrier. config.json already
            # used `_kasa_note` for exactly this, and YAML comments do not
            # survive a round trip through a parser, so the convention stays.
            if isinstance(k, str) and k.startswith("_"):
                continue
            if k not in schema:
                near = _nearest(k, schema)
                errors.append("%s: unknown key %r%s"
                              % (path or "(root)", k,
                                 (" -- did you mean %r?" % near) if near else ""))
                continue
            _validate(v, schema[k], _join(path, k), errors)
        return

    if isinstance(schema, tuple) and schema and schema[0] == "list":
        if not isinstance(node, list):
            errors.append("%s: expected a list, got %s"
                          % (path, type(node).__name__))
            return
        for i, item in enumerate(node):
            _validate(item, schema[1], "%s[%d]" % (path, i), errors)
        return

    if isinstance(schema, tuple) and schema and schema[0] == "map":
        if not isinstance(node, dict):
            errors.append("%s: expected a mapping, got %s"
                          % (path, type(node).__name__))
            return
        for k, v in node.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            _validate(v, schema[1], _join(path, k), errors)
        return

    # Scalar. An explicit null is always allowed by the schema -- it means
    # "deliberately nothing", and it is the ACCESSORS that decide whether a
    # given caller may accept one.
    if node is None:
        return
    types = schema if isinstance(schema, tuple) else (schema,)
    # bool is a subclass of int; an int field must not silently accept `true`.
    if bool not in types and isinstance(node, bool):
        errors.append("%s: expected %s, got bool" % (path, _typename(schema)))
        return
    if not isinstance(node, types):
        errors.append("%s: expected %s, got %s"
                      % (path, _typename(schema), type(node).__name__))


def _nearest(key, schema):
    """Cheap did-you-mean. A typo'd key is the common case this catches, and
    naming the near miss is the difference between a refusal that helps and one
    that just stops."""
    try:
        import difflib
        m = difflib.get_close_matches(key, [k for k in schema], 1, 0.75)
        return m[0] if m else None
    except Exception:
        return None


def _join(path, key):
    return "%s.%s" % (path, key) if path else str(key)


class Config(object):
    """An immutable resolved configuration.

    `source` says where it came from, and is part of the object because
    "which file is this daemon actually running on" is a question that must be
    answerable from the running process rather than by re-reading the disk. A
    config file is not the configuration.
    """

    def __init__(self, data, source=None, warnings=None):
        self._data = data
        self.source = source
        self.warnings = tuple(warnings or ())

    # -- the three accessors ------------------------------------------------

    def optional(self, path):
        """ABSENT, NONE, or the value. The caller branches on all three."""
        node = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return ABSENT
            node = node[part]
        return NONE if node is None else node

    def require(self, path):
        """The value. ConfigError if absent or explicitly null."""
        v = self.optional(path)
        if v is ABSENT:
            raise ConfigError("required setting %r is not configured%s"
                              % (path, self._where()))
        if v is NONE:
            raise ConfigError("required setting %r is explicitly null%s"
                              % (path, self._where()))
        return v

    def default(self, path, fallback):
        """The value, or `fallback` when the key is ABSENT.

        An explicit null RAISES rather than falling back. "Deliberately
        nothing" and "unspecified" are different instructions, and this is the
        accessor a hurried caller reaches for, so it is the one that must not
        be able to flatten them. A caller that genuinely wants to treat null as
        a fallback can say so with optional().
        """
        v = self.optional(path)
        if v is ABSENT:
            return fallback
        if v is NONE:
            raise ConfigError(
                "%r is explicitly null%s -- that is not the same as unset. "
                "Use optional() and handle NONE if a deliberate 'nothing' is "
                "meaningful here." % (path, self._where()))
        return v

    def secret(self, path):
        """Read a secret named indirectly by `<path>_env`.

        Passwords are never literals in the file. The file names an environment
        variable; this reads it. Returns ABSENT when no variable is named, and
        raises when one is named and not set -- a named-but-missing secret is a
        misconfiguration, not an absence.
        """
        var = self.optional(path + "_env")
        if var is ABSENT or var is NONE:
            return ABSENT
        val = os.environ.get(var)
        if val is None:
            raise ConfigError("%s_env names %r, which is not set in the "
                              "environment" % (path, var))
        return val

    # -- reporting ----------------------------------------------------------

    def as_dict(self):
        """A deep copy, for serving in status output. Copied rather than
        returned directly so a consumer cannot mutate the running config."""
        import copy
        return copy.deepcopy(self._data)

    def _where(self):
        return " (config: %s)" % self.source if self.source else \
               " (no config file loaded; built-in defaults only)"

    def __repr__(self):
        return "<Config source=%r>" % (self.source,)


def find_config_file(explicit=None):
    """First hit wins. Returns None when there is no file anywhere, which is a
    supported state: the daemon runs on built-in defaults."""
    if explicit:
        return explicit
    env = os.environ.get("VCCTRL_CONFIG")
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "vcctrl.yaml"),
                 os.path.expanduser("~/.config/vcctrl/vcctrl.yaml"),
                 "/opt/vcctrl/vcctrl.yaml"):
        if os.path.exists(cand):
            return cand
    return None


def _read_yaml(path):
    try:
        import yaml
    except ImportError:
        raise ConfigError(
            "PyYAML is not installed, so %s cannot be read. "
            "Install it with: apt install python3-yaml" % path)
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    except Exception as exc:
        raise ConfigError("%s is not valid YAML: %s" % (path, exc))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("%s must contain a mapping at the top level, got %s"
                          % (path, type(data).__name__))
    return data


def _apply_env(data):
    """Overlay VCCTRL_* onto the merged tree. Returns the list of paths that
    were overridden, so `config show` can say which values did not come from
    the file -- an override nobody can see is how two people end up debugging
    different configurations."""
    applied = []
    for var, path in ENV_OVERRIDES.items():
        raw = os.environ.get(var)
        if raw is None:
            continue
        val = raw
        if path in _INT_PATHS:
            try:
                val = int(raw)
            except ValueError:
                raise ConfigError("%s=%r is not an integer (overrides %s)"
                                  % (var, raw, path))
        node = data
        parts = path.split(".")
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = val
        applied.append("%s <- %s" % (path, var))
    return applied


def load(path=None, strict=True):
    """Resolve the configuration once.

    `strict` False downgrades schema violations to warnings. The daemon uses
    strict=True and catches ConfigError at start-up so a bad file is reported
    rather than fatal; `vcctrl config check` uses strict=True and reports.
    Nothing uses strict=False casually -- it exists so a tool that only needs
    one field is not blocked by an unrelated typo elsewhere in the file.
    """
    src = find_config_file(path)
    filedata = _read_yaml(src) if src else {}

    merged = _merge(DEFAULTS, filedata)
    applied = _apply_env(merged)

    errors = []
    _validate(merged, SCHEMA, "", errors)
    if errors:
        msg = ("%s failed validation:\n  " % (src or "(built-in defaults)")
               + "\n  ".join(errors))
        if strict:
            raise ConfigError(msg)
        return Config(merged, src, warnings=errors + applied)

    return Config(merged, src, warnings=applied)


# ---------------------------------------------------------------------------
# `python3 common/vcconfig.py [check] [path]`
#
# Runnable directly so it works on the control host and the daemon host without
# either needing the other. It reports the FILE's state. The running daemon's
# resolved configuration is a different question, answered by asking the daemon
# -- a config file is not the configuration.
# ---------------------------------------------------------------------------

def _report(cfg):
    """Per capability: configured / explicitly-none / absent.

    Three states, matching what the capabilities themselves promise. A
    two-valued report here would be the same defect this module exists to
    prevent, one level up.
    """
    lines = []
    caps = cfg.optional("capabilities")
    if caps is ABSENT:
        lines.append("capabilities: absent -- every capability runs on its "
                     "built-in default")
        return lines
    if caps is NONE:
        lines.append("capabilities: explicitly none")
        return lines
    for name in sorted(caps):
        if name.startswith("_"):
            continue
        backend = cfg.optional("capabilities.%s.backend" % name)
        if backend is ABSENT:
            lines.append("  %-8s absent          (no backend named)" % name)
        elif backend is NONE:
            lines.append("  %-8s explicitly none (will answer 'not "
                         "configured', never a default)" % name)
        else:
            settings = cfg.optional("capabilities.%s.settings" % name)
            n = len(settings) if isinstance(settings, dict) else 0
            lines.append("  %-8s configured      backend=%s, %d setting%s"
                         % (name, backend, n, "" if n == 1 else "s"))
    return lines


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    path = args[1] if len(args) > 1 else None
    try:
        cfg = load(path)
    except ConfigError as exc:
        sys.stderr.write("vcctrl config: %s\n" % exc)
        return 1

    print("source:   %s" % (cfg.source or
                            "(none found -- built-in defaults only)"))
    print("rig:      %s" % (cfg.default("rig.name", "(unnamed)")))
    for w in cfg.warnings:
        print("override: %s" % w)
    print("")
    for line in _report(cfg):
        print(line)

    targets = cfg.optional("targets")
    print("")
    if targets is ABSENT or targets is NONE:
        print("targets:  none configured -- board identity will not resolve "
              "to a machine name")
    else:
        for t in targets:
            print("  board %-3s %-20s leds=%s"
                  % (t.get("board_id", "?"), t.get("name", "(unnamed)"),
                     t.get("leds", "unknown")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
