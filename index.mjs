import { readFile } from "node:fs/promises";
import path from "node:path";

function manifest(value) {
  if (value?.version !== 1 || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(value.host)
      || typeof value.title !== "string" || !value.title || !path.isAbsolute(value.state_dir)
      || !/^codex-ssh-[0-9a-f]+$/.test(value.provider)
      || !Number.isFinite(value.stale_after_seconds) || value.stale_after_seconds < 15) {
    throw new Error("Invalid SSH monitor configuration. Run python3 manage.py add HOST.");
  }
  const encoded = Buffer.from(value.host, "ascii").toString("hex");
  if (value.provider !== `codex-ssh-${encoded}`) throw new Error("SSH host and provider identity do not match.");
  return value;
}

function quota(snapshot, monitor, now = Date.now()) {
  if (snapshot.version !== 1 || snapshot.host !== monitor.host) throw new Error("Snapshot belongs to another SSH server.");
  if (snapshot.error) return { error: snapshot.error, windows: [] };
  const collected = Date.parse(snapshot.collected_at);
  if (!Number.isFinite(collected) || now - collected > monitor.stale_after_seconds * 1000) {
    return { error: "Collector has stopped updating. Check manage.py status HOST.", windows: [] };
  }
  if (snapshot.catching_up) return { error: "Reading initial remote history…", windows: [] };
  const windows = [];
  for (const event of snapshot.quotas ?? []) {
    const source = new Date(event.t);
    if (!Number.isFinite(source.getTime())) throw new Error("Invalid quota observation time.");
    const observed = source.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
    for (const key of ["primary", "secondary"]) {
      const window = event.limits[key];
      if (window == null) continue;
      const { used_percent: used, window_minutes: minutes, resets_at: reset } = window;
      if (typeof used !== "number" || !Number.isFinite(used) || used < 0
          || !Number.isFinite(minutes) || minutes <= 0 || !Number.isFinite(reset)) throw new Error("Invalid quota window.");
      if (reset * 1000 <= now) continue;
      const label = minutes === 10080 ? "Weekly" : minutes === 300 ? "5 hours" : `${minutes} minutes`;
      windows.push({ name: `${event.limits.limit_id ?? "codex"} · ${label}`, used, span: minutes * 60,
        resetsAt: new Date(reset * 1000).toISOString(),
        display: `${used}% used · log snapshot ${observed}`, aside: true });
    }
  }
  if (!windows.length) return { error: "No unexpired quota snapshot. Use Codex on the SSH server, then sync.", windows: [] };
  return { plan: `${monitor.title} · log snapshot`, user: `${monitor.host} (SSH)`, windows };
}

export const CodexSSHMonitor = async () => {
  let monitor;
  try { monitor = manifest(JSON.parse(await readFile(new URL("./monitor.json", import.meta.url), "utf8"))); }
  catch (error) { throw new Error(`Configure this plugin with: python3 manage.py add HOST. ${error.message}`); }
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[monitor.provider] = { name: `${monitor.title} · Codex SSH`, models: {} };
    },
    auth: {
      provider: monitor.provider,
      methods: [{ type: "api", label: "SSH alias (no API key needed)", placeholder: monitor.host }],
      loader: async () => ({ fetch: async () => { throw new Error("Read-only monitor; no model inference endpoint."); } }),
      async usage(getAuth) {
        const auth = await getAuth();
        if (auth?.key !== monitor.host) return { error: `Set the SSH alias to ${monitor.host}.`, windows: [] };
        try { return quota(JSON.parse(await readFile(path.join(monitor.state_dir, "snapshot.json"), "utf8")), monitor); }
        catch (error) { return { error: `${monitor.title}: ${error.message}`, windows: [] }; }
      },
    },
  };
};

export const _internal = { manifest, quota };
