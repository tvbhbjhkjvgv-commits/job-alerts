// Reads the text of a resume and suggests a candidate's settings from it.
// Pure functions, used by the settings page (in the browser) and by tests/test_page.js (in Node).
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ResumeParse = factory();
})(this, function () {
  "use strict";

  var MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, sept: 8, oct: 9, nov: 10, dec: 11 };
  var NOT_A_NAME = /resume|curriculum|vitae|\bcv\b|profile|engineer|developer|analyst|email|e-mail|phone|mobile|linkedin|github|address|summary|objective/i;
  var STUDY_LINE = /b\.?\s?tech|b\.?\s?e\b|bachelor|master|m\.?\s?tech|mca|bca|b\.?sc|m\.?sc|university|college|school|institute|intermediate|ssc|hsc|cgpa|gpa|percentage/i;

  function compile(source) {
    try {
      return new RegExp(source, "i");
    } catch (error) {
      return null; // a pattern this browser cannot read is skipped, not fatal
    }
  }

  function findEmails(text) {
    var found = text.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [];
    var seen = {};
    return found.filter(function (address) {
      var key = address.toLowerCase();
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  function titleCase(words) {
    return words.toLowerCase().replace(/(^|[\s.'-])([a-z])/g, function (all, before, letter) { return before + letter.toUpperCase(); });
  }

  // The candidate's name: the first short line near the top that is only words.
  function findName(text, fileName) {
    var lines = text.split(/\r?\n/).map(function (line) { return line.replace(/\s+/g, " ").trim(); }).filter(Boolean).slice(0, 10);
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].split(/\s*[|,•·]\s*/)[0];
      var words = line.split(" ");
      if (words.length >= 2 && words.length <= 4 && /^[A-Za-z][A-Za-z.'\- ]+$/.test(line) && !NOT_A_NAME.test(line)) return titleCase(line);
    }
    var fromFile = String(fileName || "").replace(/\.[A-Za-z0-9]+$/, "").replace(/[_\-.()]+/g, " ")
      .replace(/\b(resume|cv|curriculum|vitae|updated|final|latest|new|copy)\b/gi, " ").replace(/\d+/g, " ").replace(/\s+/g, " ").trim();
    return fromFile ? titleCase(fromFile) : "";
  }

  function toMonth(month, year) {
    var index = month ? MONTHS[String(month).toLowerCase().slice(0, 4)] : undefined;
    if (index === undefined && month) index = MONTHS[String(month).toLowerCase().slice(0, 3)];
    if (index === undefined && /^\d{1,2}$/.test(String(month || ""))) index = Number(month) - 1;
    return Number(year) * 12 + (index === undefined ? 0 : index);
  }

  // Months worked, from the date ranges in the experience part of the resume ("Jan 2022 - Present").
  function monthsFromDates(text, today) {
    var lines = text.split(/\r?\n/);
    var start = -1, end = lines.length;
    for (var i = 0; i < lines.length; i++) {
      var heading = lines[i].replace(/[^A-Za-z ]/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
      if (start < 0 && heading.length < 40 && /^(?:[a-z]+ ){0,2}(?:experience|history)$|^work experience|^professional experience|^employment/.test(heading)) {
        start = i;
      } else if (start >= 0 && i > start && /^(?:education|academic|projects?|personal projects|skills|technical skills|certifications?|achievements|awards|languages|declaration)/.test(heading) && heading.length < 40) {
        end = i;
        break;
      }
    }
    var scoped = start >= 0;
    var part = scoped ? lines.slice(start, end) : lines;
    var point = "(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\\.?[\\s,'-]*|(\\d{1,2})\\s*[/.-]\\s*)?((?:19|20)\\d{2})";
    var range = new RegExp(point + "\\s*(?:-|–|—|to|till|until)+\\s*(?:(present|current|now|till\\s*date|to\\s*date|ongoing)|" + point + ")", "gi");
    var now = today.getFullYear() * 12 + today.getMonth();
    var spans = [];
    part.forEach(function (line, index) {
      // With no "Experience" heading to go by, dates on or just under a degree or college line are study years.
      if (!scoped && (STUDY_LINE.test(line) || STUDY_LINE.test(part[index - 1] || "") || STUDY_LINE.test(part[index - 2] || ""))) return;
      var match;
      range.lastIndex = 0;
      while ((match = range.exec(line)) !== null) {
        var from = toMonth(match[1] || match[2], match[3]);
        var to = match[4] ? now : toMonth(match[5] || match[6], match[7]) + (match[5] || match[6] ? 1 : 12);
        if (!match[1] && !match[2] && !match[4]) to = Number(match[7]) * 12 + 12; // "2021 - 2023" covers both years
        if (to > now + 12) continue; // a range that ends in the future is a course, not a job
        if (to > now) to = now;
        if (to > from && to - from <= 600) spans.push([from, to]);
      }
    });
    spans.sort(function (a, b) { return a[0] - b[0]; });
    var total = 0, reach = -Infinity;
    spans.forEach(function (span) { // jobs that overlap are counted once
      var from = Math.max(span[0], reach);
      if (span[1] > from) total += span[1] - from;
      reach = Math.max(reach, span[1]);
    });
    return total;
  }

  // Years of experience, two ways: the figure the resume states ("1+ year of experience") and the sum of
  // its job dates. A resume written a while ago states less than its dates now add up to, so the page
  // shows both and starts from the larger. null where the resume gives nothing to go on.
  function findYears(text, today) {
    var top = text.slice(0, 2500);
    var match = /(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b[^.\n]{0,60}?\b(?:experience|exp)\b/i.exec(top)
      || /\b(?:experience|exp)\b[^.\n\d]{0,30}(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b/i.exec(top);
    var stated = match && Number(match[1]) <= 45 ? Number(match[1]) : null;
    var months = monthsFromDates(text, today || new Date());
    var fromDates = months > 0 ? Math.round(months / 6) / 2 : null; // to the nearest half year
    var student = /\bfresher\b|\bfresh graduate\b|\bundergraduate\b|\bfinal[- ]year\b|\bpursuing\b/i.test(top);
    var years = stated === null && fromDates === null ? (student ? 0 : null) : Math.max(stated || 0, fromDates || 0);
    if (student && stated === null) years = 0; // a student's dates are internships and courses
    return { years: years, stated: stated, fromDates: fromDates, student: student };
  }

  function findSkills(text, skills) {
    var found = [];
    Object.keys(skills).forEach(function (name) {
      var hit = skills[name].some(function (source) {
        var pattern = compile(source);
        return pattern !== null && pattern.test(text);
      });
      if (hit) found.push(name);
    });
    return found;
  }

  function findCities(text, cities) {
    var found = [];
    Object.keys(cities).forEach(function (city) {
      var first = -1;
      cities[city].forEach(function (name) {
        var at = text.search(new RegExp("\\b" + name.replace(/ /g, "\\s+") + "\\b", "i"));
        if (at >= 0 && (first < 0 || at < first)) first = at;
      });
      if (first >= 0) found.push([first, city]);
    });
    found.sort(function (a, b) { return a[0] - b[0]; });
    return found.slice(0, 3).map(function (entry) { return entry[1]; });
  }

  function count(text, source) {
    var pattern;
    try { pattern = new RegExp(source, "gi"); } catch (error) { return 0; }
    return (text.match(pattern) || []).length;
  }

  // Roles to search for: named outright in the resume counts most, then the skills that point to a role.
  function suggestRoles(text, roles) {
    var scored = roles.map(function (role) {
      var named = count(text, role.title), signals = Math.min(6, count(text, role.signals));
      return { name: role.name, named: named, score: 6 * named + signals };
    });
    scored.sort(function (a, b) { return b.score - a.score; });
    var named = scored.filter(function (role) { return role.named > 0; });
    // Skills alone are a weak hint ("Docker" does not make a DevOps engineer), so they only fill in
    // when the resume names fewer than two roles.
    var hinted = scored.filter(function (role) { return role.named === 0 && role.score >= 3; });
    var picked = named.length >= 2 ? named.slice(0, 4) : named.concat(hinted).slice(0, 3);
    return picked.map(function (role) { return role.name; });
  }

  // Everything the page pre-fills from one resume.
  function extract(text, data, fileName, today) {
    var clean = String(text || "").replace(/ /g, " ");
    var skills = findSkills(clean, data.skills);
    var roles = suggestRoles(clean, data.roles);
    var pythonRoles = ["Python Developer", "AI Engineer", "Generative AI Engineer", "Backend Developer", "Full Stack Developer", "Software Engineer", "Machine Learning Engineer"];
    var pythonFirst = skills.indexOf("python") >= 0 && roles.length > 0 && pythonRoles.indexOf(roles[0]) >= 0
      && roles.indexOf("Java Developer") < 0 && roles.indexOf(".NET Developer") < 0;
    return {
      name: findName(clean, fileName),
      emails: findEmails(clean).slice(0, 1),
      years: findYears(clean, today).years,
      yearsDetail: findYears(clean, today),
      skills: skills,
      roles: roles,
      cities: findCities(clean, data.cities),
      pythonRules: pythonFirst,
    };
  }

  return { extract: extract, findEmails: findEmails, findName: findName, findYears: findYears, findSkills: findSkills,
           findCities: findCities, suggestRoles: suggestRoles, compile: compile };
});
