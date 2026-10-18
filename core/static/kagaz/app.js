/*
 * Kagaz web app: the chat, the PIN sheet, downloads and small helpers.
 * Plain JavaScript, no build step. Every form also works without it.
 */
(function () {
  "use strict";

  var csrfMeta = document.querySelector("meta[name=csrf-token]");
  var csrfToken = csrfMeta ? csrfMeta.content : "";
  var chat = document.querySelector("[data-chat]");
  var thread = chat && chat.querySelector("[data-thread]");
  var entries = chat && chat.querySelector("[data-entries]");
  var typing = chat && chat.querySelector("[data-typing]");
  var pinSheet = document.getElementById("pin-sheet");
  var optionsSheet = document.getElementById("options-sheet");
  var afterUnlock = null;

  // --- Small helpers --------------------------------------------------------

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function toFragment(html) {
    var template = document.createElement("template");
    template.innerHTML = html.trim();
    return template.content;
  }

  function clockTime() {
    return new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }

  function sizeLabel(bytes) {
    return bytes < 1048576 ? Math.max(1, Math.round(bytes / 1024)) + " KB" : (bytes / 1048576).toFixed(1) + " MB";
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", "X-CSRFToken": csrfToken }
    }).then(function (response) {
      if (response.status === 401) {  // Signed out (idle timeout): show the sign-in page.
        window.location.reload();
        throw new Error("signed-out");
      }
      return response;
    });
  }

  function readJson(response) {
    return response.json().catch(function () { return {}; });
  }

  function toast(text, kind) {
    var list = document.querySelector("[data-toasts]");
    if (!list) {
      list = document.createElement("ul");
      list.className = "toasts";
      list.setAttribute("data-toasts", "");
      document.body.appendChild(list);
    }
    var item = document.createElement("li");
    item.className = "toast " + (kind || "");
    item.setAttribute("role", "status");
    item.textContent = text;
    list.appendChild(item);
    setTimeout(function () { item.remove(); }, 6000);
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  // Toasts rendered by the server fade away on their own.
  document.querySelectorAll("[data-toasts] .toast").forEach(function (item) {
    setTimeout(function () { item.remove(); }, 6000);
  });

  // --- PIN sheet -------------------------------------------------------------

  function askPin(then) {
    if (!pinSheet) { window.location.href = "/unlock/?next=" + encodeURIComponent(location.pathname); return; }
    afterUnlock = then || null;
    var form = pinSheet.querySelector("form");
    form.reset();
    pinSheet.querySelector("[data-pin-error]").textContent = "";
    openDialog(pinSheet);
    form.querySelector("input[name=pin]").focus();
  }

  if (pinSheet) {
    pinSheet.querySelector("form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.target;
      var button = form.querySelector("button[type=submit]");
      var error = pinSheet.querySelector("[data-pin-error]");
      button.classList.add("busy");
      post(form.action, new FormData(form)).then(function (response) {
        return readJson(response).then(function (body) {
          if (body.ok) {
            closeDialog(pinSheet);
            var next = afterUnlock;
            afterUnlock = null;
            // Finish what was waiting (e.g. a download), then reload so hidden
            // numbers and the lock in the header update. The chat is kept.
            Promise.resolve(next && next()).then(function () {
              setTimeout(function () { window.location.reload(); }, next ? 700 : 0);
            });
            return;
          }
          error.textContent = body.error || "That didn't work. Please try again.";
          form.querySelector("input[name=pin]").select();
          if (body.locked) setTimeout(function () { window.location.reload(); }, 2500);
        });
      }).catch(function (err) {
        if (err.message !== "signed-out") error.textContent = "Couldn't check your PIN. Check your internet and try again.";
      }).then(function () { button.classList.remove("busy"); });
    });
  }

  // --- Downloads ---------------------------------------------------------------

  function fileNameFrom(response) {
    var header = response.headers.get("Content-Disposition") || "";
    var encoded = /filename\*=utf-8''([^;]+)/i.exec(header);
    if (encoded) return decodeURIComponent(encoded[1]);
    var plain = /filename="([^"]+)"/i.exec(header);
    return plain ? plain[1] : "Kagaz_document";
  }

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
  }

  function download(form, button) {
    if (button) button.classList.add("busy");
    return post(form.action, new FormData(form)).then(function (response) {
      if (response.status === 403) {
        return readJson(response).then(function (body) {
          if (body.error === "pin") askPin(function () { return download(form, button); });
        });
      }
      if (!response.ok) {
        return readJson(response).then(function (body) {
          toast(body.error || "Couldn't prepare that file. Please try again.", "error");
        });
      }
      var note = response.headers.get("X-Kagaz-Note");
      return response.blob().then(function (blob) {
        saveBlob(blob, fileNameFrom(response));
        if (note) toast(decodeURIComponent(note), "warning");
      });
    }).catch(function (err) {
      if (err.message !== "signed-out") toast("Couldn't download. Check your internet and try again.", "error");
    }).then(function () {
      if (button) button.classList.remove("busy");
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("[data-download]")) return;
    event.preventDefault();
    if (form.matches("[data-options-form]")) prepareOptions(form);
    download(form, event.submitter || form.querySelector("button[type=submit]"));
    if (form.matches("[data-options-form]")) closeDialog(optionsSheet);
  });

  // --- Options sheet -------------------------------------------------------------

  var optionsCard = null;

  function openOptions(card) {
    optionsCard = card;
    var form = optionsSheet.querySelector("form");
    form.reset();
    optionsSheet.querySelector("[data-options-name]").textContent = card.getAttribute("data-name") || "";
    optionsSheet.querySelector("[data-sides]").hidden = !card.hasAttribute("data-front");
    openDialog(optionsSheet);
  }

  function prepareOptions(form) {
    var holder = form.querySelector("[data-options-docs]");
    holder.innerHTML = "";
    var ids = (optionsCard.getAttribute("data-ids") || "").split(",").filter(Boolean);
    var side = (form.querySelector("input[name=sides]:checked") || {}).value;
    if (side === "front") ids = [optionsCard.getAttribute("data-front")];
    if (side === "back") ids = [optionsCard.getAttribute("data-back")];
    ids.forEach(function (id) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "doc";
      input.value = id;
      holder.appendChild(input);
    });
  }

  // --- Chat --------------------------------------------------------------------

  function scrollToEnd(smooth) {
    if (thread) thread.scrollTo({ top: thread.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  function showTyping(on) {
    if (!typing) return;
    typing.hidden = !on;
    if (on) scrollToEnd(true);
  }

  function myBubble(innerHtml) {
    var previous = entries.lastElementChild;
    var first = !previous || !previous.classList.contains("me");
    var node = toFragment(
      '<div class="msg me new pending' + (first ? " first" : "") + '"><div class="bubble">' + innerHtml +
      '<span class="meta">' + clockTime() + '<svg class="icon" aria-label="Sending"><use href="#i-clock"></use></svg></span>' +
      '<span class="clear"></span></div></div>'
    ).firstElementChild;
    entries.appendChild(node);
    scrollToEnd(true);
    return node;
  }

  function markSent(node) {
    if (!node) return;
    node.classList.remove("pending");
    var icon = node.querySelector(".meta use");
    if (icon) icon.setAttribute("href", "#i-checks");
  }

  function botSays(text) {
    entries.appendChild(toFragment(
      '<div class="msg bot first new"><div class="bubble"><span class="text">' + escapeHtml(text) +
      '</span><span class="meta">' + clockTime() + '</span><span class="clear"></span></div></div>'
    ));
    scrollToEnd(true);
  }

  function talk(url, data, mine) {
    showTyping(true);
    return post(url, data).then(function (response) {
      if (!response.ok) throw new Error("http-" + response.status);
      return response.text();
    }).then(function (html) {
      markSent(mine);
      showTyping(false);
      entries.appendChild(toFragment(html));
      scrollToEnd(true);
    }).catch(function (err) {
      showTyping(false);
      if (err.message === "signed-out") return;
      if (mine) mine.classList.add("failed");
      botSays("I couldn't get that through. Check your internet and try again.");
    });
  }

  function sendText(text) {
    text = (text || "").trim();
    if (!text || !chat) return;
    var mine = myBubble('<span class="text">' + escapeHtml(text) + "</span>");
    var data = new FormData();
    data.append("text", text);
    return talk(chat.getAttribute("data-send-url"), data, mine);
  }

  function sendAction(button) {
    var label = button.getAttribute("data-label") || "";
    var mine = label ? myBubble('<span class="text">' + escapeHtml(label) + "</span>") : null;
    var data = new FormData();
    data.append("action", button.getAttribute("data-action"));
    data.append("doc", button.getAttribute("data-doc") || "");
    data.append("value", button.getAttribute("data-value") || "");
    data.append("label", label);
    var group = button.closest(".chips");
    if (group && !group.closest(".suggest")) group.classList.add("used");  // Answer chips are tapped once.
    return talk(chat.getAttribute("data-send-url"), data, mine);
  }

  function upload(file) {
    var limit = parseInt(chat.getAttribute("data-max-upload"), 10);
    if (file.size > limit) {
      botSays("That file is too big. I can save files up to " + chat.getAttribute("data-max-upload-mb") + " MB.");
      return;
    }
    var mine = myBubble(
      '<div class="doc"><span class="doc-badge tone-file"><b>FILE</b></span><span class="doc-body">' +
      '<span class="doc-title">' + escapeHtml(file.name) + '</span><span class="doc-meta">' + sizeLabel(file.size) +
      " · uploading…</span></span></div>"
    );
    var data = new FormData();
    data.append("file", file);
    return talk(chat.getAttribute("data-upload-url"), data, mine).then(function () {
      var meta = mine.querySelector(".doc-meta");
      if (meta && !mine.classList.contains("failed")) meta.textContent = sizeLabel(file.size);
    });
  }

  if (chat) {
    var composer = chat.querySelector("[data-composer]");
    var textarea = composer.querySelector("textarea");
    var fileInput = chat.querySelector("[data-file]");
    var touch = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;

    entries.querySelectorAll(".msg.new").forEach(function (node) { node.classList.remove("new"); });
    scrollToEnd(false);

    function grow() {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
    }

    composer.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = textarea.value;
      textarea.value = "";
      grow();
      sendText(text);
      textarea.focus();
    });

    textarea.addEventListener("input", grow);
    textarea.addEventListener("keydown", function (event) {
      // Desktop: Enter sends, Shift+Enter is a new line. Phones: the send button sends.
      if (event.key === "Enter" && !event.shiftKey && !touch && !event.isComposing) {
        event.preventDefault();
        if (composer.requestSubmit) composer.requestSubmit();
        else composer.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });

    fileInput.addEventListener("change", function () {
      if (fileInput.files[0]) upload(fileInput.files[0]);
      fileInput.value = "";
    });
  }

  // --- Clicks anywhere ---------------------------------------------------------

  document.addEventListener("click", function (event) {
    var target = event.target.closest(
      "[data-send],[data-action],[data-attach],[data-pin],[data-copy],[data-options],[data-fill],[data-close]"
    );
    if (!target) {
      // Close the header menu when tapping elsewhere.
      document.querySelectorAll("details.menu[open]").forEach(function (menu) {
        if (!menu.contains(event.target)) menu.removeAttribute("open");
      });
      return;
    }
    var menu = target.closest("details.menu");
    if (menu && !target.closest("form")) menu.removeAttribute("open");

    if (target.hasAttribute("data-send") && chat) {
      event.preventDefault();
      sendText(target.getAttribute("data-send"));
    } else if (target.hasAttribute("data-action") && chat) {
      event.preventDefault();
      sendAction(target);
    } else if (target.hasAttribute("data-attach") && chat) {
      event.preventDefault();
      chat.querySelector("[data-file]").click();
    } else if (target.hasAttribute("data-pin")) {
      event.preventDefault();
      askPin(null);
    } else if (target.hasAttribute("data-copy")) {
      var scope = target.closest("[data-copy-scope]");
      var value = scope && scope.querySelector("[data-copy-value]");
      if (value && navigator.clipboard) {
        navigator.clipboard.writeText(value.textContent.trim()).then(function () {
          target.lastChild.textContent = "Copied";
          setTimeout(function () { target.lastChild.textContent = "Copy"; }, 1800);
        });
      }
    } else if (target.hasAttribute("data-options") && optionsSheet) {
      openOptions(target.closest("[data-card]"));
    } else if (target.hasAttribute("data-fill")) {
      var input = target.closest("form").querySelector("input[name=target_kb]");
      input.value = target.getAttribute("data-fill");
    } else if (target.hasAttribute("data-close")) {
      closeDialog(target.closest("dialog"));
    }
  });
})();
