// Host glossary save. A failed legacy POST must not look like a successful write.
// The textarea speaks zh|alias=en. Aliases that round-trip stay editable.
// Lock off, a note, a category, text the line would change, or more than 40 rows do not.

const LEGACY_BOX_LIMIT = 40;
const LEGACY_EDIT_PLACE = "請用 PUT /api/rooms/{room_id}/glossary 修改。這是技術操作，請找負責詞表的人。格式見 README「修改房間術語表」。";
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

function glossaryLineBreaks(text) {
  return /[\n\r\u0085\u2028\u2029]/.test(text);
}

// The left side is split on | and the line is split on the first = or ＝.
// English is everything after that first equals, so = | ＝ inside en still round-trip.
function glossaryLeftSeparators(text) {
  return /[|=\uFF1D]/.test(text) || glossaryLineBreaks(text);
}

function glossaryTermUnexpressable(term) {
  if (!term || typeof term !== "object") return false;
  if (term.lock === false) return true;
  if (typeof term.note === "string" && term.note.trim()) return true;
  if (typeof term.category === "string" && term.category.trim()) return true;
  const zh = typeof term.zh === "string" ? term.zh : "";
  const en = typeof term.en === "string" ? term.en : "";
  if (zh.trim() !== zh || en.trim() !== en || glossaryLeftSeparators(zh) || glossaryLineBreaks(en)) return true;
  // A leading # is a comment in the textarea, so the canonical would not round-trip.
  if (zh.startsWith("#")) return true;
  const aliases = Array.isArray(term.aliases) ? term.aliases : [];
  for (const alias of aliases) {
    if (typeof alias !== "string" || !alias) continue;
    if (alias.trim() !== alias || !alias.trim() || glossaryLeftSeparators(alias)) return true;
  }
  return false;
}

export function glossaryHasAdvanced(terms) {
  for (const term of terms || []) {
    if (glossaryTermUnexpressable(term)) return true;
  }
  return false;
}

function glossaryRoomName(room) {
  return String(room || "").trim() || "class";
}

function glossaryEditPlace(room) {
  if (room == null) return LEGACY_EDIT_PLACE;
  return LEGACY_EDIT_PLACE.replace("{room_id}", glossaryRoomName(room));
}

function glossaryPrefillNote(count) {
  return "這個房間已經存了 " + count + " 條術語，但文字框裡是開頁前留下的內容，跟已存的不一樣。為了不蓋掉那 " + count + " 條，現在不能儲存。要看已存的詞表：先把文字框裡想留的詞複製起來，清空文字框，再重新整理頁面。";
}

function glossaryRefused(note) {
  if (!note || note.startsWith("沒有儲存：")) return note || "";
  return "沒有儲存：" + note;
}

export function glossaryLegacyBlock(terms, room) {
  const list = Array.isArray(terms) ? terms : [];
  const advanced = glossaryHasAdvanced(list);
  const over = list.length > LEGACY_BOX_LIMIT;
  const limit = "（主持頁最多 " + LEGACY_BOX_LIMIT + " 條）";
  const place = glossaryEditPlace(room);
  if (over && advanced) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，而且" + LEGACY_RICH + "，這裡只能看、不能改。" + place,
    };
  }
  if (over) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，這裡只能看、不能改。" + place,
    };
  }
  if (advanced) {
    return {
      locked: true,
      note: "這個房間的術語表" + LEGACY_RICH + "，這裡只能看、不能改。" + place,
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
    // True when the box held text before the first load of a non-empty glossary.
    // That text must not be given the server version.
    prefillUnversioned: false,
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
  return { ...state, loadedText: state.text, version: null, prefillUnversioned: false };
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
    prefillUnversioned: false,
  };
}

export function glossaryLoadTargetsSelector(requestedRoom, selectorRoom) {
  return glossaryRoomName(requestedRoom) === glossaryRoomName(selectorRoom);
}

// A GET for a room the selector is not showing must not clear the box.
export function glossaryBeginLoad(state, requestedRoom, selectorRoom) {
  if (!glossaryLoadTargetsSelector(requestedRoom, selectorRoom)) {
    return { started: false, state, generation: state.generation };
  }
  const next = glossaryPrepareLoad(state, requestedRoom);
  return {
    started: true,
    state: next,
    generation: next.generation,
    room: glossaryRoomName(requestedRoom),
  };
}

export function glossaryApplyLoaded(state, room, generation, terms, version) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const block = glossaryLegacyBlock(terms, nextRoom);
  const serverText = glossaryBoxText(terms);
  const dirty = state.text !== state.loadedText;
  const count = Array.isArray(terms) ? terms.length : 0;
  if (dirty && state.room === nextRoom) {
    if (state.prefillUnversioned) {
      return { ...state, version: null, note: glossaryPrefillNote(count) };
    }
    return {
      ...state,
      note: "這個文字框有還沒儲存的修改，沒有用已經存好的詞蓋掉。",
    };
  }
  // First paint can already hold typed or browser-restored text. Pairing that
  // text with the server version lets a save replace terms the host has not seen.
  if (dirty && state.room == null && !block.locked) {
    if (count > 0 && state.text !== serverText) {
      return {
        room: nextRoom,
        version: null,
        locked: false,
        text: state.text,
        loadedText: serverText,
        note: glossaryPrefillNote(count),
        readOnly: false,
        generation,
        pendingRoom: nextRoom,
        prefillUnversioned: true,
      };
    }
    return {
      room: nextRoom,
      version,
      locked: false,
      text: state.text,
      loadedText: serverText,
      note: "這個文字框有還沒儲存的修改，沒有用已經存好的詞蓋掉。",
      readOnly: false,
      generation,
      pendingRoom: nextRoom,
      prefillUnversioned: false,
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
    prefillUnversioned: false,
  };
}

export function glossaryApplyLoadFailure(state, room, generation) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const dirty = state.text !== state.loadedText;
  if (state.room === nextRoom || (state.room == null && dirty)) {
    const note = dirty
      ? "讀不到這個房間已經存的術語。文字框裡還沒儲存的修改還在。"
      : "讀不到這個房間已經存的術語，畫面上仍是上次看到的內容。";
    return { ...state, note };
  }
  return {
    room: null,
    version: null,
    locked: true,
    text: "",
    loadedText: "",
    note: "讀不到這個房間已經存的術語，所以這裡先空白。",
    readOnly: true,
    generation,
    pendingRoom: nextRoom,
    prefillUnversioned: false,
  };
}

export function glossarySaveDecision(state, selectorRoom, sessionId) {
  const target = glossaryRoomName(selectorRoom);
  if (!state.room || target !== state.room) return { post: false, reason: "room-mismatch" };
  if (state.locked) return { post: false, reason: "locked" };
  return glossarySaveRequest(state.room, sessionId, state.text, state.version, false);
}

// A 200 applies only when the box still shows that room. Another room is left
// untouched, including its text and version. The same room with a newer
// generation means a reconnect loaded during the POST: remember the posted
// text, drop the stale version, and reload. Otherwise the next save sends the
// old if_version and the server answers 409.
function glossaryRememberPosted(state, sentText) {
  const loadedText = sentText == null ? state.text : String(sentText);
  return { ...state, loadedText, version: null, prefillUnversioned: false };
}

export function glossarySaveSettlement(state, requestRoom, requestGeneration, sentText) {
  const room = glossaryRoomName(requestRoom);
  if (!state || state.room !== room) {
    return { settle: false, state };
  }
  if (state.generation !== requestGeneration) {
    return { settle: true, state: glossaryRememberPosted(state, sentText), reloadRoom: room };
  }
  return { settle: true, state: glossaryMarkSaved(state), reloadRoom: room };
}

export function glossaryRefusal(state, reason) {
  if (reason === "locked") {
    return (state && state.note) || ("這裡只能看、不能改。" + glossaryEditPlace(state && state.room));
  }
  if (reason === "room-mismatch") {
    if (state && state.note) return glossaryRefused(state.note);
    return "沒有儲存：文字框裡的詞不是這個房間的。";
  }
  if (reason === "no-version" && state && state.prefillUnversioned && state.note) {
    return glossaryRefused(state.note);
  }
  return "還沒讀到這個房間目前存的術語，請重新整理頁面後再儲存。";
}
