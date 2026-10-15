import assert from "node:assert/strict";
import { test } from "node:test";
import { _internal } from "../index.mjs";

const now = Date.parse("2026-01-10T10:00:00Z");
const monitor = (host = "devbox", stale = 180) => _internal.manifest({ version: 1, host, title: "Development",
  state_dir: "/tmp/private-monitor", provider: `codex-ssh-${Buffer.from(host).toString("hex")}`, stale_after_seconds: stale });
const snapshot = () => ({ version: 1, host: "devbox", collected_at: new Date(now).toISOString(),
  quotas: [{ t: "2026-01-09T08:00:00Z", limits: { limit_id: "codex", primary: {
    used_percent: 38, window_minutes: 10080, resets_at: (now + 86400000) / 1000 } } }] });

test("window duration is read from source, not assumed from primary", () => {
  const card = _internal.quota(snapshot(), monitor(), now);
  assert.match(card.windows[0].name, /Weekly/);
  assert.match(card.windows[0].display, /2026-01-09 08:00:00 UTC/);
  assert.equal(card.windows[0].used, 38);
});
test("another server's snapshot is refused", () => {
  assert.throws(() => _internal.quota(snapshot(), monitor("buildbox"), now), /another SSH server/);
});
test("freshness follows a custom poll interval", () => {
  assert.equal(_internal.quota(snapshot(), monitor("devbox", 1800), now + 1000000).windows.length, 1);
  assert.equal(_internal.quota(snapshot(), monitor(), now + 181000).windows.length, 0);
});
test("collector errors hide stale quota windows", () => {
  const value = snapshot(); value.error = "SSH refused the connection";
  assert.equal(_internal.quota(value, monitor(), now).error, value.error);
  assert.equal(_internal.quota(value, monitor(), now).windows.length, 0);
});
test("expired windows are not invented as unused quota", () => {
  const value = snapshot(); value.quotas[0].limits.primary.resets_at = (now - 1) / 1000;
  assert.equal(_internal.quota(value, monitor(), now).windows.length, 0);
});
test("provider identity cannot be reused for another host", () => {
  const value = monitor(); value.host = "buildbox";
  assert.throws(() => _internal.manifest(value), /identity do not match/);
});
