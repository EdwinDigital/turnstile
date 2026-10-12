import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join } from "node:path";

export async function privateJSON(path, value) {
  const temporary = path + "." + randomUUID() + ".tmp";
  try {
    await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally { await unlink(temporary).catch(() => {}); }
}

export async function importBootstrap(input, cfg) {
  if (input.project?.id !== "magpie" || !input.directory || !cfg.authorization) return false;
  const auth = cfg.authorization;
  if (auth.type !== "oauth" || auth.uid !== cfg.userId || auth.accountId !== cfg.userId
      || !auth.access?.startsWith("tsca_") || !auth.refresh?.startsWith("tscr_")
      || !Number.isFinite(auth.expires) || auth.installationId !== cfg.installationId)
    throw new Error("Invalid downloaded authorization");
  const path = join(input.directory, "plugin-auth.json");
  const marker = join(input.directory, "aigateway-proxy-user", cfg.providerId, "bootstrap-imported.json");
  let lock;
  try {
    try { await readFile(marker); return false; }
    catch (error) { if (error.code !== "ENOENT") return false; }
    const deadline = Date.now() + 2000;
    while (!lock) {
      try { lock = await open(path + ".lock", "wx", 0o600); }
      catch (error) {
        if (error.code !== "EEXIST" || Date.now() >= deadline) return false;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    }
    let previous;
    try { previous = await readFile(path, "utf8"); }
    catch (error) { if (error.code !== "ENOENT") return false; }
    const accounts = previous === undefined ? {} : JSON.parse(previous);
    if (!accounts || typeof accounts !== "object" || Array.isArray(accounts)) return false;
    await mkdir(join(input.directory, "aigateway-proxy-user", cfg.providerId), { recursive: true, mode: 0o700 });
    // Mark before writing so logout and old downloads cannot resurrect an account.
    const mark = await open(marker, "wx", 0o600).catch(error => {
      if (error.code === "EEXIST") return null;
      throw error;
    });
    if (!mark) return false;
    try { await mark.writeFile("{}"); await mark.sync(); } finally { await mark.close(); }
    if (Object.keys(accounts).some(id => id === cfg.providerId || id.startsWith(cfg.providerId + "#"))) return false;
    accounts[cfg.providerId] = auth;
    if (previous !== undefined) await privateJSON(path + ".bak", JSON.parse(previous));
    await privateJSON(path, accounts);
    return true;
  } catch { return false; }
  finally { if (lock) { await lock.close(); await unlink(path + ".lock").catch(() => {}); } }
}

export function adaptModelResponse(response, alias, expected) {
  if (!alias || !expected || !response.ok || !response.body) return response;
  const replace = (value) => {
    if (value?.model === expected) value.model = alias;
    if (value?.response?.model === expected) value.response.model = alias;
    if (value?.message?.model === expected) value.message.model = alias;
    return value;
  };
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  const type = headers.get("content-type") ?? "";
  if (type.includes("text/event-stream")) {
    let pending = "";
    const decoder = new TextDecoder();
    const encoder = new TextEncoder();
    const consume = (controller, final = false) => {
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(pending))) {
        const frame = pending.slice(0, boundary.index);
        pending = pending.slice(boundary.index + boundary[0].length);
        const adapted = frame.split(/\r?\n/).map((line) => {
          if (!line.startsWith("data:")) return line;
          try { return "data: " + JSON.stringify(replace(JSON.parse(line.slice(5)))); }
          catch { return line; }
        }).join("\n");
        controller.enqueue(encoder.encode(adapted + "\n\n"));
      }
      if (pending.length > 4 * 1024 * 1024) throw new Error("Upstream SSE event exceeds the client limit");
      if (final && pending) controller.enqueue(encoder.encode(pending));
    };
    const stream = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) { pending += decoder.decode(chunk, { stream: true }); consume(controller); },
      flush(controller) { pending += decoder.decode(); consume(controller, true); },
    }));
    return new Response(stream, { status: response.status, statusText: response.statusText, headers });
  }
  if (!type.includes("application/json")) return response;
  const stream = new ReadableStream({
    async start(controller) {
      try {
        const reader = response.body.getReader();
        const chunks = []; let size = 0;
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          size += value.length; if (size > 16 * 1024 * 1024) throw new Error("Upstream JSON exceeds the client limit");
          chunks.push(value);
        }
        const bytes = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
        let result = bytes;
        try { result = new TextEncoder().encode(JSON.stringify(replace(JSON.parse(new TextDecoder().decode(bytes))))); }
        catch { /* Non-JSON errors keep their original bytes. */ }
        controller.enqueue(result); controller.close();
      } catch (error) { controller.error(error); }
    },
    cancel(reason) { return response.body.cancel(reason); },
  });
  return new Response(stream, { status: response.status, statusText: response.statusText, headers });
}

