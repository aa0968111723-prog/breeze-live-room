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
  if (!details.length) return "術語沒有寫入這個房間。";
  return "術語沒有寫入這個房間。\n" + details.join("\n");
}
