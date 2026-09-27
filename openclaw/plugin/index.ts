// violet-bridge: OpenClaw owns Telegram; Violet (Python) does all the work.
//
// Every private Telegram message is claimed at reply_dispatch (so OpenClaw's model never runs) and posted to
// Violet's local bridge endpoint with the sender id OpenClaw's Telegram adapter supplies, the text, the
// message id and any attachments (already staged as local files). Button taps on Violet's buttons (callback
// data "vb:<payload>") are forwarded the same way. Violet sends every reply itself through the Gateway's
// message tool, so the plugin never replies. It fails closed: if Violet is unreachable nothing is sent and
// no model runs. It never logs message content.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const BRIDGE_URL = process.env.VIOLET_BRIDGE_URL || "http://127.0.0.1:8765";
const BRIDGE_TOKEN = process.env.VIOLET_BRIDGE_TOKEN || "";
const TIMEOUT_MS = 170_000; // Violet may take a while (voice notes, documents); it answers the chat itself

async function forward(kind: string, body: Record<string, unknown>, log: { warn: (m: string) => void }) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${BRIDGE_URL}/openclaw/${kind}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${BRIDGE_TOKEN}` },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    if (!res.ok) log.warn(`violet bridge answered ${res.status} for a ${kind}`);
  } catch (e) {
    log.warn(`violet bridge unreachable for a ${kind} (${(e as Error).name})`);
  } finally {
    clearTimeout(timer);
  }
}

function telegramId(v: unknown): string | undefined {
  const s = String(v ?? "");
  const m = s.match(/(-?\d+)$/);
  return m ? m[1] : undefined;
}

export default definePluginEntry({
  id: "violet-bridge",
  name: "Violet bridge",
  description: "Forwards Telegram messages and button taps to Violet; no model involved.",
  register(api) {
    const log = api.logger;

    api.on(
      "reply_dispatch",
      async (event: any, hctx: any) => {
        const ctx = event.ctx ?? {};
        const senderId = telegramId(ctx.SenderId) ?? telegramId(ctx.From);
        const isPrivate = !ctx.ChatType || ctx.ChatType === "direct" || ctx.ChatType === "private";
        const claimed = { handled: true, queuedFinal: false, counts: { tool: 0, block: 0, final: 0 } };
        if (ctx.OriginatingChannel && ctx.OriginatingChannel !== "telegram") return claimed;
        if (!senderId || !isPrivate) return claimed; // groups and unknown senders: silently dropped
        const media = (ctx.media ?? []).map((m: any) => ({
          path: m.path, contentType: m.contentType, kind: m.kind, fileName: m.fileName, durationMs: m.durationMs,
        }));
        // Show "typing…" while Violet works (OpenClaw's typing loop, refreshed every few seconds).
        try { await hctx?.onReplyStart?.(); } catch { /* typing is best effort */ }
        await forward("message", {
          senderId,
          chatId: telegramId(ctx.OriginatingTo) ?? telegramId(ctx.To) ?? senderId,
          messageId: ctx.MessageSid ?? null,
          text: ctx.RawBody ?? ctx.CommandBody ?? ctx.Body ?? "",
          firstName: ctx.SenderName ?? null,
          username: ctx.SenderUsername ?? null,
          media,
        }, log);
        log.info(`forwarded a message from ${senderId}${media.length ? ` with ${media.length} attachment(s)` : ""}`);
        try { hctx?.markIdle?.("violet replied"); } catch { /* stops typing */ }
        return claimed;
      },
      { priority: 1000 },
    );

    // Belt and braces: if anything ever reaches the agent path, answer with silence (never a model reply).
    api.on("before_agent_reply", () => ({ handled: true }), { priority: 1000 });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vb",
      handler: async (ctx: any) => {
        if (ctx.isGroup || !ctx.senderId) return { handled: true };
        await forward("callback", {
          senderId: String(ctx.senderId),
          chatId: String(ctx.callback.chatId),
          messageId: ctx.callback.messageId,
          callbackId: ctx.callbackId,
          data: ctx.callback.payload,
          messageText: ctx.callback.messageText ?? null,
        }, log);
        return { handled: true };
      },
    });
  },
});
