// Host glossary save. A failed legacy POST must not look like a successful write.
// The textarea speaks zh|alias=en. Aliases that round-trip stay editable.
// Lock off, a note, a category, text the line would change, or more than 40 rows do not.

const LEGACY_BOX_LIMIT = 40;
const LEGACY_EDIT_PLACE = "請用 PUT /api/rooms/{room_id}/glossary 修改";
const LEGACY_RICH = "含備註、分類或未鎖定的詞，或文字框無法原樣表示的內容";

export function glossarySaveLine(status, body) {
  if (Number(status) === 200) {
    const deleted = body && Number(body.deleted);
    if (Number.isInteger(deleted) && deleted > 0) {
      return "已儲存這個房間的術語。這次刪了 " + deleted + " 條。";
    }
    return "已儲存這個房間的術語。";
  }
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

function glossarySeparators(text) {
  return /[|=\uFF1D\n\r\u0085\u2028\u2029]/.test(text);
}

function glossaryTermUnexpressable(term) {
  if (!term || typeof term !== "object") return false;
  if (term.lock === false) return true;
  if (typeof term.note === "string" && term.note.trim()) return true;
  if (typeof term.category === "string" && term.category.trim()) return true;
  const zh = typeof term.zh === "string" ? term.zh : "";
  const en = typeof term.en === "string" ? term.en : "";
  if (zh.trim() !== zh || en.trim() !== en || glossarySeparators(zh) || glossarySeparators(en)) return true;
  const aliases = Array.isArray(term.aliases) ? term.aliases : [];
  for (const alias of aliases) {
    if (typeof alias !== "string" || !alias) continue;
    if (alias.trim() !== alias || !alias.trim() || glossarySeparators(alias)) return true;
  }
  return false;
}

export function glossaryHasAdvanced(terms) {
  for (const term of terms || []) {
    if (glossaryTermUnexpressable(term)) return true;
  }
  return false;
}

export function glossaryLegacyBlock(terms) {
  const list = Array.isArray(terms) ? terms : [];
  const advanced = glossaryHasAdvanced(list);
  const over = list.length > LEGACY_BOX_LIMIT;
  const limit = "（主持頁最多 " + LEGACY_BOX_LIMIT + " 條）";
  if (over && advanced) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，而且" + LEGACY_RICH + "，這裡只能看、不能改。" + LEGACY_EDIT_PLACE,
    };
  }
  if (over) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，這裡只能看、不能改。" + LEGACY_EDIT_PLACE,
    };
  }
  if (advanced) {
    return {
      locked: true,
      note: "這個房間的術語表" + LEGACY_RICH + "，這裡只能看、不能改。" + LEGACY_EDIT_PLACE,
    };
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

export function glossaryRoomState() {
  return {
    room: null,
    version: null,
    locked: false,
    text: "",
    loadedText: "",
    note: "",
    readOnly: false,
    generation: 0,
    pendingRoom: null,
  };
}

export function glossaryTextDirty(state) {
  return !!state && state.text !== state.loadedText;
}

export function glossaryEdit(state, text) {
  return { ...state, text: String(text ?? "") };
}

export function glossaryMarkSaved(state) {
  // Drop the version until the following load reads the one the server just wrote.
  return { ...state, loadedText: state.text, version: null };
}

function glossaryRoomName(room) {
  return String(room || "").trim() || "class";
}

export function glossaryPrepareLoad(state, room) {
  const nextRoom = glossaryRoomName(room);
  const generation = state.generation + 1;
  if (state.room === nextRoom || state.room == null) {
    return { ...state, generation, pendingRoom: nextRoom };
  }
  return {
    room: null,
    version: null,
    locked: true,
    text: "",
    loadedText: "",
    note: "正在讀取這個房間的術語。",
    readOnly: true,
    generation,
    pendingRoom: nextRoom,
  };
}

export function glossaryApplyLoaded(state, room, generation, terms, version) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const block = glossaryLegacyBlock(terms);
  const serverText = glossaryBoxText(terms);
  const dirty = state.text !== state.loadedText;
  if (dirty && state.room === nextRoom) {
    return {
      ...state,
      note: "這個文字框有還沒儲存的修改，沒有用伺服器上的詞表覆蓋。",
    };
  }
  if (dirty && state.room == null && !block.locked) {
    return {
      room: nextRoom,
      version,
      locked: false,
      text: state.text,
      loadedText: serverText,
      note: "這個文字框有還沒儲存的修改，沒有用伺服器上的詞表覆蓋。",
      readOnly: false,
      generation,
      pendingRoom: nextRoom,
    };
  }
  return {
    room: nextRoom,
    version,
    locked: block.locked,
    text: serverText,
    loadedText: serverText,
    note: block.note || "",
    readOnly: block.locked,
    generation,
    pendingRoom: nextRoom,
  };
}

export function glossaryApplyLoadFailure(state, room, generation) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const dirty = state.text !== state.loadedText;
  if (state.room === nextRoom || (state.room == null && dirty)) {
    const note = dirty
      ? "讀不到伺服器上的術語表。文字框裡還沒儲存的修改還在。"
      : "讀不到伺服器上的術語表，畫面上仍是上次載入的內容。";
    return { ...state, note };
  }
  return {
    room: null,
    version: null,
    locked: true,
    text: "",
    loadedText: "",
    note: "讀不到這個房間的術語表，沒有寫入。",
    readOnly: true,
    generation,
    pendingRoom: nextRoom,
  };
}

export function glossarySaveDecision(state, selectorRoom, sessionId) {
  const target = glossaryRoomName(selectorRoom);
  if (!state.room || target !== state.room) return { post: false, reason: "room-mismatch" };
  if (state.locked) return { post: false, reason: "locked" };
  return glossarySaveRequest(state.room, sessionId, state.text, state.version, false);
}

export function glossaryRefusal(state, reason) {
  if (reason === "locked") {
    return (state && state.note) || ("這裡只能看、不能改。" + LEGACY_EDIT_PLACE);
  }
  if (reason === "room-mismatch") {
    return (state && state.note) || "文字框裡的術語不是這個房間的，沒有寫入。";
  }
  return "讀不到目前的術語表版本，請重新整理頁面後再儲存。";
}
