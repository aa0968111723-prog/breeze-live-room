// Host glossary save. A failed legacy POST must not look like a successful write.

export function glossarySaveLine(status, body) {
  if (Number(status) === 200) return "已儲存這個房間的術語。";
  const rejected = body && Array.isArray(body.rejected) ? body.rejected : [];
  const details = [];
  for (const item of rejected) {
    if (!item || typeof item !== "object") continue;
    const reason = typeof item.reason === "string" ? item.reason : "";
    if (!reason) continue;
    const lineNo = Number(item.line);
    details.push(lineNo > 0 ? "第 " + lineNo + " 行：" + reason : reason);
  }
  const lines = ["術語沒有寫入這個房間。"];
  if (Number(status) === 409) lines.push("術語表已更新，請重新整理頁面後再儲存。");
  lines.push(...details);
  if (lines.length === 1) return lines[0];
  return lines.join("\n");
}

export function glossaryTransportLine(err) {
  const detail = err && typeof err.message === "string" && err.message ? "（" + err.message + "）" : "";
  return "術語沒有寫入這個房間。" + detail;
}

export function glossaryPostBody(room, sessionId, text, version) {
  const body = {
    room_id: room,
    session_id: sessionId,
    text: String(text ?? ""),
  };
  if (Number.isInteger(version)) body.if_version = version;
  return body;
}
