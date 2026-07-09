const form = document.querySelector("#searchForm");
const locationInput = document.querySelector("#location");
const radiusInput = document.querySelector("#radius");
const sourceInput = document.querySelector("#source");
const queriesInput = document.querySelector("#queries");
const fetchEmailsInput = document.querySelector("#fetchEmails");
const statusEl = document.querySelector("#status");
const keyStateEl = document.querySelector("#keyState");
const runButton = document.querySelector("#runButton");
const rowsEl = document.querySelector("#rows");
const resultCountEl = document.querySelector("#resultCount");
const xlsxLink = document.querySelector("#xlsxLink");
const pdfLink = document.querySelector("#pdfLink");
const csvLink = document.querySelector("#csvLink");
const jsonLink = document.querySelector("#jsonLink");
const filterInput = document.querySelector("#filter");
const loadingOverlay = document.querySelector("#loadingOverlay");

let rows = [];

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text || "";
  return td;
}

function linkCell(url, label) {
  const td = document.createElement("td");
  if (!url) {
    td.textContent = label || "";
    return td;
  }
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noreferrer";
  a.textContent = label || url;
  td.appendChild(a);
  return td;
}

function render() {
  const term = filterInput.value.trim().toLowerCase();
  const shown = rows.filter((row) => JSON.stringify(row).toLowerCase().includes(term));
  rowsEl.textContent = "";
  resultCountEl.textContent = `${shown.length} sonuç`;

  if (!shown.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "empty";
    td.textContent = rows.length ? "Filtreye uyan sonuç yok." : "Henüz arama yapılmadı.";
    tr.appendChild(td);
    rowsEl.appendChild(tr);
    return;
  }

  for (const row of shown) {
    const tr = document.createElement("tr");
    tr.appendChild(linkCell(row.google_maps_url, row.name || "Google Maps"));
    tr.appendChild(cell(row.phone));
    tr.appendChild(cell(row.address));
    tr.appendChild(linkCell(row.website, row.website ? "Site" : ""));
    tr.appendChild(cell(row.emails));
    tr.appendChild(cell(row.distance_km));
    tr.appendChild(cell(row.rating ? `${row.rating} (${row.reviews || 0})` : ""));
    rowsEl.appendChild(tr);
  }
}

function enableDownload(link, href) {
  link.href = href || "#";
  link.classList.toggle("disabled", !href);
}

pdfLink.target = "_blank";
pdfLink.rel = "noreferrer";

function setLoading(isLoading) {
  runButton.disabled = isLoading;
  loadingOverlay.classList.toggle("active", isLoading);
  loadingOverlay.setAttribute("aria-hidden", String(!isLoading));
  document.body.classList.toggle("loading", isLoading);
}

async function loadConfig() {
  const response = await fetch("/config");
  const config = await response.json();
  locationInput.value = config.location || "";
  radiusInput.value = config.radius_km || 10;
  sourceInput.value = config.source || "osm";
  queriesInput.value = config.queries || "";
  fetchEmailsInput.checked = Boolean(config.fetch_emails);
  keyStateEl.textContent = config.source === "google" && !config.has_api_key ? "Google API anahtarı bekleniyor" : "API'siz kaynak hazır";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("Aranıyor...");
  setLoading(true);
  enableDownload(xlsxLink, "");
  enableDownload(pdfLink, "");
  enableDownload(csvLink, "");
  enableDownload(jsonLink, "");

  try {
    const response = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: locationInput.value,
        radius_km: Number(radiusInput.value),
        source: sourceInput.value,
        queries: queriesInput.value,
        fetch_emails: fetchEmailsInput.checked,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Arama tamamlanamadı.");

    rows = data.rows || [];
    render();
    enableDownload(xlsxLink, data.files && data.files.xlsx);
    enableDownload(pdfLink, data.files && data.files.pdf);
    enableDownload(csvLink, data.files && data.files.csv);
    enableDownload(jsonLink, data.files && data.files.json);
    setStatus(`${data.count} firma bulundu.`);
  } catch (error) {
    rows = [];
    render();
    setStatus(error.message, true);
  } finally {
    setLoading(false);
  }
});

filterInput.addEventListener("input", render);

loadConfig().catch((error) => {
  keyStateEl.textContent = "Config okunamadı";
  setStatus(error.message, true);
});
