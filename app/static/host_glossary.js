// Host glossary save. A failed legacy POST must not look like a successful write.
// The textarea only speaks zh|alias=en. Anything else stays on the server.

const LEGACY_BOX_LIMIT = 40;

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

export function glossaryBoxText(terms) {
  const lines = [];
  for (const term of terms || []) {
    if (!term || typeof term !== "object") continue;
    const zh = typeof term.zh === "string" ? term.zh : "";
    const en = typeof term.en === "string" ? term.en : "";
    const aliases = Array.isArray(term.aliases)
      ? term.aliases.filter((alias) => typeof alias === "string" && alias)
      : [];
    const left = aliases.length ? [zh, ...aliases].join("|") : zh;
    lines.push(left + "=" + en);
  }
  return lines.join("\n");
}

export function glossaryHasAdvanced(terms) {
  for (const term of terms || []) {
    if (!term || typeof term !== "object") continue;
    if (term.lock === false) return true;
    if (typeof term.note === "string" && term.note.trim()) return true;
    if (typeof term.category === "string" && term.category.trim()) return true;
    const aliases = Array.isArray(term.aliases) ? term.aliases : [];
    if (aliases.some((alias) => typeof alias === "string" && alias.trim())) return true;
  }
  return false;
}

export function glossaryLegacyBlock(terms) {
  const list = Array.isArray(terms) ? terms : [];
  const advanced = glossaryHasAdvanced(list);
  const over = list.length > LEGACY_BOX_LIMIT;
  if (over && advanced) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條／含進階欄位，請用術語表編輯器修改",
    };
  }
  if (over) {
    return { locked: true, note: "這個房間的術語表有 " + list.length + " 條，請用術語表編輯器修改" };
  }
  if (advanced) {
    return { locked: true, note: "這個房間的術語表含進階欄位，請用術語表編輯器修改" };
  }
  return { locked: false, note: "" };
}

export function glossarySaveRequest(room, sessionId, text, loadedVersion, locked) {
  if (locked) return { post: false, reason: "locked" };
  if (!String(text ?? "").trim()) return { post: false, reason: "empty" };
  // loadedVersion is the version captured when the box was filled, not a fresh GET.
  if (!Number.isInteger(loadedVersion)) return { post: false, reason: "no-version" };
  return { post: true, body: glossaryPostBody(room, sessionId, text, loadedVersion) };
}
