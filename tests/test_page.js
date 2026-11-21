// Offline checks of the settings page's resume reader (docs/parse.js) against docs/data.js.
// Usage: node tests/test_page.js
"use strict";

const path = require("path");
const Parse = require(path.join(__dirname, "..", "docs", "parse.js"));
const DATA = require(path.join(__dirname, "..", "docs", "data.js"));

const failures = [];
function check(name, condition, detail) {
  console.log((condition ? "PASS " : "FAIL ") + name + (!condition && detail ? " | " + detail : ""));
  if (!condition) failures.push(name);
}

const TODAY = new Date(2026, 10, 21);

const unreadable = [];
for (const [name, patterns] of Object.entries(DATA.skills)) {
  for (const pattern of patterns) if (!Parse.compile(pattern)) unreadable.push(name + ": " + pattern);
}
check("every skill pattern from the scan code works in JavaScript", unreadable.length === 0, unreadable.join(" | "));
check("the page knows the job families, cities and companies", DATA.stacks.includes("Java") && DATA.stacks.includes("QA / testing")
  && DATA.cities.Bengaluru.includes("bangalore") && DATA.companies.infosys === "Infosys");

const WORKING = [
  "Asha Rao",
  "asha.rao@example.com | +91 90000 00000 | Bangalore, India",
  "Summary",
  "Data Analyst with 1+ year of experience in reporting and ETL pipelines.",
  "Experience",
  "Data Analyst - Acme Finance, Bangalore                May 2024 - Present",
  "Built Power BI dashboards with DAX; SQL pipelines on Apache Airflow.",
  "Skills",
  "Python (Pandas, NumPy), SQL, Power BI, Excel, Git",
  "Education",
  "B.Tech, Some College of Engineering        2020 - 2024",
].join("\n");
const working = Parse.extract(WORKING, DATA, "asha_resume.pdf", TODAY);
check("name and email come from the top of the resume", working.name === "Asha Rao" && working.emails.join() === "asha.rao@example.com", working.name);
check("years: the stated figure and the job dates are both reported, and the larger one is offered",
  working.yearsDetail.stated === 1 && working.yearsDetail.fromDates === 2.5 && working.years === 2.5, JSON.stringify(working.yearsDetail));
check("study years are not counted as work", Parse.findYears(WORKING, TODAY).fromDates === 2.5);
check("skills are found under the scan's own names", ["python", "pandas", "sql", "power bi", "excel", "airflow", "dax", "git"].every((s) => working.skills.includes(s)),
  working.skills.join(", "));
check("the role named in the resume leads; the city is given its usual spelling",
  working.roles[0] === "Data Analyst" && working.cities.join() === "Bengaluru" && working.pythonRules === false, JSON.stringify([working.roles, working.cities]));

const STUDENT = [
  "PRIYA K",
  "priya@example.com",
  "Professional Summary",
  "Motivated Computer Science undergraduate with hands-on experience in Python, Flask and Machine Learning.",
  "Internship Experience",
  "Web Development Intern, EduCo",
  "Built pages with HTML, CSS and Flask.",
  "Education",
  "B.Tech (Computer Science)",
  "Some College of Engineering",
  "2023 – 2027",
].join("\n");
const student = Parse.extract(STUDENT, DATA, "priya.pdf", TODAY);
check("a student's resume gives 0 years, not the length of the degree", student.years === 0 && student.yearsDetail.student === true, JSON.stringify(student.yearsDetail));
check("a name in capitals is put in ordinary case", student.name === "Priya K", student.name);

const DEVELOPER = [
  "Python Developer",
  "Ravi Teja",
  "ravi@example.com",
  "Python Developer with 3 years of experience building REST APIs with Django and FastAPI on PostgreSQL and Docker.",
  "Worked as a Backend Developer on LangChain and RAG features.",
].join("\n");
const developer = Parse.extract(DEVELOPER, DATA, "", TODAY);
check("a heading that is a job title is not taken for the name", developer.name === "Ravi Teja", developer.name);
check("a Python developer gets the Python rules and the stated years", developer.pythonRules === true && developer.years === 3
  && developer.roles.includes("Python Developer") && developer.roles.includes("Backend Developer"), JSON.stringify([developer.years, developer.roles]));
check("with no name in the text the file name is used", Parse.findName("resume\nemail: x@example.com", "Asha_Rao_Resume (1).pdf") === "Asha Rao");
check("a resume with no years and no dates leaves the years empty", Parse.findYears("Sales executive. Good communication.", TODAY).years === null);

console.log();
if (failures.length) {
  console.log(failures.length + " FAILED: " + failures.join("; "));
  process.exit(1);
}
console.log("ALL PASS");
