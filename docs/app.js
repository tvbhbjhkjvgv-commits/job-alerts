// The settings page: one candidate per file (candidates/<id>.json) in the private settings repository.
// Everything runs in the browser. The only requests go to api.github.com, with the token typed in here.
(function () {
  "use strict";

  var DATA = window.JOB_ALERTS_DATA;
  var Parse = window.ResumeParse;
  var API = "https://api.github.com/repos/";
  var PDF_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  var state = { repo: "", token: "", connected: false, people: [], current: null };
  var chips = {};

  function $(id) { return document.getElementById(id); }

  function remember(key, value) {
    try {
      if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
    } catch (error) { /* a private window: the page still works, it just forgets */ }
  }

  function recall(key) {
    try { return localStorage.getItem(key) || ""; } catch (error) { return ""; }
  }

  function setStatus(id, text, kind) {
    var box = $(id);
    box.textContent = text || "";
    box.className = "status" + (kind ? " " + kind : "");
  }

  // ---- chip fields (emails, roles, skills, cities) ---------------------------------------------
  function makeChips(box) {
    var values = [];
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = box.dataset.placeholder || "";
    input.setAttribute("aria-label", box.dataset.field.replace(/_/g, " "));
    if (box.dataset.list) input.setAttribute("list", box.dataset.list);
    box.appendChild(input);
    var listeners = [];

    function draw() {
      Array.prototype.slice.call(box.querySelectorAll(".chip")).forEach(function (chip) { chip.remove(); });
      values.forEach(function (value, index) {
        var chip = document.createElement("span");
        chip.className = "chip";
        chip.appendChild(document.createTextNode(value));
        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", "Remove " + value);
        remove.addEventListener("click", function () { values.splice(index, 1); draw(); changed(); });
        chip.appendChild(remove);
        box.insertBefore(chip, input);
      });
    }

    function changed() { listeners.forEach(function (listener) { listener(values.slice()); }); }

    function add(raw) {
      var before = values.length;
      String(raw || "").split(/[,;\n]+/).forEach(function (piece) {
        var value = piece.replace(/\s+/g, " ").trim();
        if (box.dataset.lower) value = value.toLowerCase();
        var known = values.some(function (existing) { return existing.toLowerCase() === value.toLowerCase(); });
        if (value && !known) values.push(value);
      });
      if (values.length !== before) { draw(); changed(); }
    }

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        add(input.value);
        input.value = "";
      } else if (event.key === "Backspace" && !input.value && values.length) {
        values.pop();
        draw();
        changed();
      }
    });
    input.addEventListener("change", function () { add(input.value); input.value = ""; });
    input.addEventListener("blur", function () { add(input.value); input.value = ""; });
    box.addEventListener("click", function (event) { if (event.target === box) input.focus(); });

    return {
      get: function () { return values.slice(); },
      set: function (list) { values = []; (list || []).forEach(function (item) { add(item); }); draw(); changed(); },
      add: add,
      onChange: function (listener) { listeners.push(listener); },
    };
  }

  function checkGrid(box, entries, prefix) {
    entries.forEach(function (entry) {
      var label = document.createElement("label");
      label.className = "check";
      var tick = document.createElement("input");
      tick.type = "checkbox";
      tick.value = entry.value;
      tick.id = prefix + "-" + entry.value.replace(/[^A-Za-z0-9]+/g, "-");
      var text = document.createElement("span");
      text.textContent = entry.label;
      label.appendChild(tick);
      label.appendChild(text);
      box.appendChild(label);
    });
  }

  function ticked(box) {
    return Array.prototype.slice.call(box.querySelectorAll("input:checked")).map(function (tick) { return tick.value; });
  }

  function setTicked(box, list) {
    Array.prototype.slice.call(box.querySelectorAll("input")).forEach(function (tick) { tick.checked = (list || []).indexOf(tick.value) >= 0; });
  }

  function fillDatalist(id, items) {
    var list = $(id);
    items.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item;
      list.appendChild(option);
    });
  }

  // The fast alert watches one of the candidate's cities.
  function syncFastCity(cities, keep) {
    var select = $("fast_city");
    var wanted = keep || select.value;
    select.textContent = "";
    cities.forEach(function (city) {
      var option = document.createElement("option");
      option.value = option.textContent = city;
      select.appendChild(option);
    });
    if (cities.indexOf(wanted) >= 0) select.value = wanted;
    var hadNoCity = $("fast_enabled").disabled;
    $("fast_enabled").disabled = cities.length === 0;
    if (cities.length === 0) $("fast_enabled").checked = false;
    // A new candidate gets the fast alert as soon as there is a city to watch.
    else if (hadNoCity && state.current && state.current.isNew) $("fast_enabled").checked = true;
  }

  function syncStacks() {
    var python = $("python_rules").checked;
    Array.prototype.slice.call($("stacks").querySelectorAll("input")).forEach(function (tick) { tick.disabled = python; });
    $("stacks-note").textContent = python
      ? "The Python developer rules already leave out the other stacks, so these boxes are not used."
      : "A job whose title belongs to a ticked family is dropped, unless the title also names one of the must-have skills.";
  }

  // ---- the form -------------------------------------------------------------------------------
  function newId() {
    var letters = "abcdefghijklmnopqrstuvwxyz0123456789", out = "c";
    var random = new Uint8Array(7);
    (window.crypto || window.msCrypto).getRandomValues(random);
    for (var i = 0; i < random.length; i++) out += letters[random[i] % letters.length];
    return out;
  }

  function canonicalCity(name) {
    var lowered = String(name || "").toLowerCase().trim();
    var found = Object.keys(DATA.cities).filter(function (city) { return DATA.cities[city].indexOf(lowered) >= 0; })[0];
    return found || lowered.replace(/(^|\s)([a-z])/g, function (all, space, letter) { return space + letter.toUpperCase(); });
  }

  function showCandidate(profile, sha) {
    var isNew = !profile;
    profile = profile || {};
    var linkedin = profile.linkedin || {}, fast = profile.fast_alert || {}, sites = profile.career_sites || {};
    state.current = { id: profile.id || newId(), sha: sha || null, isNew: isNew };
    $("editor-title").textContent = isNew ? "New candidate" : (profile.name || "Candidate " + profile.id);
    $("candidate-id").textContent = "id: " + state.current.id;
    $("name").value = profile.name || "";
    $("years").value = profile.experience_years === undefined ? "" : profile.experience_years;
    $("years-hint").textContent = "";
    $("enabled").checked = profile.enabled !== false;
    chips.emails.set(profile.emails || []);
    chips.roles.set(profile.roles || []);
    chips.skills.set(profile.skills || []);
    chips.must_have.set(profile.must_have || []);
    chips.exclude_title_words.set(profile.exclude_title_words || []);
    chips.cities.set(profile.cities || []);
    $("python_rules").checked = !!profile.python_rules;
    setTicked($("stacks"), profile.avoid_stacks || []);
    $("remote_india").checked = profile.remote_india !== false;
    $("linkedin_enabled").checked = linkedin.enabled !== false;
    $("linkedin_min").value = linkedin.min_match === undefined ? DATA.defaults.linkedin : linkedin.min_match;
    $("linkedin_every").checked = !!linkedin.mail_every_run;
    syncFastCity(chips.cities.get(), fast.city);
    $("fast_enabled").checked = fast.enabled !== false && chips.cities.get().length > 0;
    $("fast_min").value = fast.min_match === undefined ? DATA.defaults.fast_alert : fast.min_match;
    $("sites_enabled").checked = sites.enabled !== false;
    $("sites_min").value = sites.min_match === undefined ? DATA.defaults.career_sites : sites.min_match;
    setTicked($("companies"), sites.companies || []);
    $("ntfy").value = profile.ntfy_topic || "";
    $("delete").hidden = isNew;
    syncStacks();
    setStatus("save-status", "");
    setStatus("resume-status", "");
    drawPeople();
  }

  function number(id, fallback) {
    var value = Number($(id).value);
    return $(id).value === "" || isNaN(value) ? fallback : Math.min(100, Math.max(0, Math.round(value)));
  }

  function readForm() {
    return {
      id: state.current.id,
      name: $("name").value.replace(/\s+/g, " ").trim(),
      emails: chips.emails.get(),
      ntfy_topic: $("ntfy").value.replace(/[^A-Za-z0-9_-]/g, ""),
      enabled: $("enabled").checked,
      experience_years: $("years").value === "" ? null : Number($("years").value),
      roles: chips.roles.get(),
      exclude_title_words: chips.exclude_title_words.get(),
      skills: chips.skills.get(),
      must_have: chips.must_have.get(),
      python_rules: $("python_rules").checked,
      avoid_stacks: $("python_rules").checked ? [] : ticked($("stacks")),
      cities: chips.cities.get().map(canonicalCity),
      remote_india: $("remote_india").checked,
      linkedin: { enabled: $("linkedin_enabled").checked, min_match: number("linkedin_min", DATA.defaults.linkedin), mail_every_run: $("linkedin_every").checked },
      fast_alert: { enabled: $("fast_enabled").checked, city: $("fast_city").value || "", min_match: number("fast_min", DATA.defaults.fast_alert) },
      career_sites: { enabled: $("sites_enabled").checked, min_match: number("sites_min", DATA.defaults.career_sites), companies: ticked($("companies")) },
    };
  }

  // The same checks the scans make when they read the file (normalise_profile in job_match_runner.py).
  function problems(profile) {
    var found = [];
    var emails = profile.emails.filter(function (address) { return /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/.test(address); });
    if (!emails.length || emails.length !== profile.emails.length) found.push("give at least one email address, and check that each one is complete");
    if (!profile.roles.length) found.push("add at least one role");
    if (!profile.skills.length) found.push("add at least one skill");
    if (!profile.cities.length && !profile.remote_india) found.push("add a city, or allow remote jobs");
    if (profile.experience_years === null || isNaN(profile.experience_years) || profile.experience_years < 0 || profile.experience_years > 50) {
      found.push("give the years of experience (0 for a fresher)");
    }
    return found;
  }

  // ---- GitHub ---------------------------------------------------------------------------------
  function explain(status, message) {
    if (status === 401) return "GitHub did not accept the token. Check that it is complete and has not expired.";
    if (status === 403) return "The token may not do this. It needs Contents: read and write on the settings repository.";
    if (status === 404) return "Repository not found, or the token has no access to it.";
    if (status === 409 || status === 422) return "The file changed since this page loaded it. Reload the page and try again.";
    return "GitHub answered " + status + (message ? ": " + message : "") + ".";
  }

  function github(path, method, body) {
    return fetch(API + state.repo + path, {
      method: method || "GET",
      headers: { "Accept": "application/vnd.github+json", "Authorization": "Bearer " + state.token, "X-GitHub-Api-Version": "2022-11-28" },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (response) {
      if (response.ok) return response.status === 204 ? null : response.json();
      return response.json().catch(function () { return {}; }).then(function (answer) {
        var error = new Error(explain(response.status, answer.message));
        error.status = response.status;
        throw error;
      });
    }, function () {
      throw new Error("Could not reach GitHub. Check the internet connection.");
    });
  }

  function encode(text) {
    var bytes = new TextEncoder().encode(text), binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function decode(base64) {
    var binary = atob(String(base64 || "").replace(/\s+/g, ""));
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  function loadPeople() {
    return github("/contents/candidates").then(function (files) {
      var wanted = (Array.isArray(files) ? files : []).filter(function (file) { return /\.json$/.test(file.name); });
      return Promise.all(wanted.map(function (file) {
        return github("/contents/candidates/" + encodeURIComponent(file.name)).then(function (item) {
          var profile;
          try { profile = JSON.parse(decode(item.content)); } catch (error) { profile = { name: file.name + " (unreadable)" }; }
          profile.id = profile.id || file.name.replace(/\.json$/, "");
          return { profile: profile, sha: item.sha };
        });
      }));
    }, function (error) {
      if (error.status === 404) return []; // no candidates folder yet: nobody has been added
      throw error;
    }).then(function (people) {
      people.sort(function (a, b) { return String(a.profile.name || "").localeCompare(String(b.profile.name || "")); });
      state.people = people;
      drawPeople();
    });
  }

  function connect() {
    state.repo = $("repo").value.trim().replace(/^https:\/\/github\.com\//, "").replace(/\/+$/, "");
    state.token = $("token").value.trim();
    if (!/^[^/\s]+\/[^/\s]+$/.test(state.repo) || !state.token) {
      setStatus("connection-status", "Fill in the settings repository (owner/name) and the token.", "bad");
      $("connection").open = true;
      return Promise.resolve();
    }
    setStatus("connection-status", "Connecting...");
    return github("").then(function () {
      state.connected = true;
      remember("job-alerts-repo", state.repo);
      remember("job-alerts-token", state.token);
      return loadPeople();
    }).then(function () {
      setStatus("connection-status", "Connected to " + state.repo + ".", "ok");
      $("connection").open = false;
    }).catch(function (error) {
      state.connected = false;
      setStatus("connection-status", error.message, "bad");
      $("connection").open = true;
    });
  }

  function drawPeople() {
    var list = $("people");
    list.textContent = "";
    state.people.forEach(function (person) {
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      if (state.current && state.current.id === person.profile.id) button.className = "current";
      var name = document.createElement("span");
      name.textContent = person.profile.name || person.profile.id;
      var note = document.createElement("small");
      note.textContent = person.profile.enabled === false ? "paused" : "";
      button.appendChild(name);
      button.appendChild(note);
      button.addEventListener("click", function () { showCandidate(person.profile, person.sha); });
      item.appendChild(button);
      list.appendChild(item);
    });
    $("people-empty").hidden = state.people.length > 0;
    $("people-empty").textContent = state.connected ? "Nobody yet. Add the first candidate." : "Connect to see the people already added.";
  }

  function save() {
    var profile = readForm();
    var found = problems(profile);
    if (found.length) {
      setStatus("save-status", "Before saving: " + found.join("; ") + ".", "bad");
      return;
    }
    if (!state.connected) {
      setStatus("save-status", "Not connected, so nothing was saved. Connect first, or use \"Download the file\".", "bad");
      $("connection").open = true;
      return;
    }
    $("save").disabled = true;
    setStatus("save-status", "Saving...");
    var path = "/contents/candidates/" + profile.id + ".json";
    var body = { message: "Update candidate " + profile.id, content: encode(JSON.stringify(profile, null, 2) + "\n") };
    if (state.current.sha) body.sha = state.current.sha;
    github(path, "PUT", body).then(function (answer) {
      state.current.sha = answer.content.sha;
      state.current.isNew = false;
      return loadPeople();
    }).then(function () {
      $("delete").hidden = false;
      $("editor-title").textContent = profile.name || "Candidate " + profile.id;
      setStatus("save-status", "Saved. The next scans use these settings.", "ok");
    }).catch(function (error) {
      setStatus("save-status", error.message, "bad");
    }).then(function () { $("save").disabled = false; });
  }

  function remove() {
    var name = $("name").value.trim() || state.current.id;
    if (!state.current.sha || !window.confirm("Delete " + name + "? Their alerts stop and their settings are removed.")) return;
    setStatus("save-status", "Deleting...");
    github("/contents/candidates/" + state.current.id + ".json", "DELETE", { message: "Remove candidate " + state.current.id, sha: state.current.sha })
      .then(loadPeople)
      .then(function () { showCandidate(null); setStatus("save-status", name + " was deleted.", "ok"); })
      .catch(function (error) { setStatus("save-status", error.message, "bad"); });
  }

  function download() {
    var profile = readForm();
    var found = problems(profile);
    if (found.length) {
      setStatus("save-status", "Before downloading: " + found.join("; ") + ".", "bad");
      return;
    }
    var link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(profile, null, 2) + "\n"], { type: "application/json" }));
    link.download = profile.id + ".json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("save-status", "Downloaded " + profile.id + ".json. Put it in the candidates folder of the settings repository.", "ok");
  }

  // ---- resumes --------------------------------------------------------------------------------
  function pdfText(buffer) {
    if (!window.pdfjsLib) return Promise.reject(new Error("The PDF reader did not load. Check the internet connection and reload the page."));
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDF_WORKER;
    return window.pdfjsLib.getDocument({ data: buffer }).promise.then(function (pdf) {
      var pages = [];
      for (var number = 1; number <= pdf.numPages; number++) pages.push(number);
      return Promise.all(pages.map(function (number) {
        return pdf.getPage(number).then(function (page) { return page.getTextContent(); }).then(function (content) {
          var text = "", lastY = null, lastEnd = null;
          content.items.forEach(function (item) {
            if (typeof item.str !== "string") return;
            var x = item.transform[4], y = item.transform[5];
            if (lastY !== null && Math.abs(y - lastY) > 2) text += "\n";
            else if (lastEnd !== null && x - lastEnd > 1 && !/\s$/.test(text) && !/^\s/.test(item.str)) text += " ";
            text += item.str;
            if (item.hasEOL) text += "\n";
            lastY = y;
            lastEnd = x + (item.width || 0);
          });
          return text;
        });
      }));
    }).then(function (pages) { return pages.join("\n"); });
  }

  function resumeText(file) {
    var name = file.name.toLowerCase();
    if (/\.pdf$/.test(name) || file.type === "application/pdf") return file.arrayBuffer().then(pdfText);
    if (/\.docx$/.test(name)) {
      if (!window.mammoth) return Promise.reject(new Error("The Word reader did not load. Check the internet connection and reload the page."));
      return file.arrayBuffer().then(function (buffer) { return window.mammoth.extractRawText({ arrayBuffer: buffer }); }).then(function (result) { return result.value; });
    }
    if (/\.doc$/.test(name)) return Promise.reject(new Error("Old .doc files cannot be read. Save the resume as PDF or .docx and drop it again."));
    return file.text();
  }

  function merge(field, found) {
    var have = chips[field].get();
    chips[field].set(have.concat(found));
  }

  function readResume(file) {
    if (!file) return;
    setStatus("resume-status", "Reading " + file.name + "...");
    resumeText(file).then(function (text) {
      if (text.replace(/\s+/g, "").length < 80) {
        throw new Error("No text could be read from " + file.name + ". It may be a scanned picture; type the details in below.");
      }
      var found = Parse.extract(text, DATA, file.name, new Date());
      if (!$("name").value.trim()) $("name").value = found.name;
      if (!chips.emails.get().length) chips.emails.set(found.emails);
      merge("skills", found.skills);
      merge("roles", found.roles);
      if (!chips.cities.get().length) chips.cities.set(found.cities);
      var detail = found.yearsDetail, hints = [];
      if ($("years").value === "" && found.years !== null) $("years").value = found.years;
      if (detail.stated !== null) hints.push("the resume states " + detail.stated + " year(s)");
      if (detail.fromDates !== null) hints.push("its job dates add up to " + detail.fromDates);
      if (detail.student) hints.push("it reads like a student's or fresher's resume");
      $("years-hint").textContent = hints.length ? "From the resume: " + hints.join("; ") + "." : "The resume gives no years; type them in.";
      if (state.current.isNew) {
        $("python_rules").checked = found.pythonRules;
        if (found.pythonRules && !chips.must_have.get().length) chips.must_have.set(["python"]);
        syncStacks();
      }
      setStatus("resume-status", "Read " + file.name + ": " + found.skills.length + " skill(s), " + found.roles.length
        + " role(s). Check every field before saving; a resume is only a starting point.", "ok");
    }).catch(function (error) {
      setStatus("resume-status", error.message || "The resume could not be read.", "bad");
    });
  }

  // ---- start ----------------------------------------------------------------------------------
  function start() {
    Array.prototype.slice.call(document.querySelectorAll(".chips")).forEach(function (box) { chips[box.dataset.field] = makeChips(box); });
    fillDatalist("skill-list", Object.keys(DATA.skills));
    fillDatalist("role-list", DATA.roles.map(function (role) { return role.name; }));
    fillDatalist("city-list", Object.keys(DATA.cities));
    checkGrid($("stacks"), DATA.stacks.map(function (name) { return { value: name, label: name }; }), "stack");
    checkGrid($("companies"), Object.keys(DATA.companies).map(function (key) { return { value: key, label: DATA.companies[key] }; }), "company");
    chips.cities.onChange(function (cities) { syncFastCity(cities.map(canonicalCity)); });
    $("python_rules").addEventListener("change", syncStacks);

    var owner = /\.github\.io$/.test(location.hostname) ? location.hostname.split(".")[0] : "";
    $("repo").value = recall("job-alerts-repo") || (owner ? owner + "/job-alerts-data" : "");
    $("token").value = recall("job-alerts-token");

    $("connect").addEventListener("click", connect);
    $("forget").addEventListener("click", function () {
      remember("job-alerts-token", null);
      $("token").value = "";
      state.token = "";
      state.connected = false;
      state.people = [];
      drawPeople();
      setStatus("connection-status", "Token forgotten.");
    });
    $("add").addEventListener("click", function () { showCandidate(null); $("name").focus(); });
    $("save").addEventListener("click", save);
    $("delete").addEventListener("click", remove);
    $("download").addEventListener("click", download);

    var drop = $("drop"), file = $("file");
    drop.addEventListener("click", function () { file.click(); });
    drop.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); file.click(); } });
    file.addEventListener("change", function () { readResume(file.files[0]); file.value = ""; });
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (event) { readResume(event.dataTransfer.files[0]); });

    showCandidate(null);
    if ($("repo").value && $("token").value) connect(); else { $("connection").open = true; drawPeople(); }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
