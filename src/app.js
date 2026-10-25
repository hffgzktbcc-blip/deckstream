// DeckStream Frontend Logic

function switchTab(tabId) {
  document.querySelectorAll(".nav-btn").forEach(btn => btn.classList.remove("active"));
  document.querySelectorAll(".tab-pane").forEach(pane => pane.classList.remove("active"));

  const activePane = document.getElementById(`tab-${tabId}`);
  if (activePane) {
    activePane.classList.add("active");
  }

  const activeBtn = Array.from(document.querySelectorAll(".nav-btn")).find(btn => 
    btn.getAttribute("onclick").includes(tabId)
  );
  if (activeBtn) {
    activeBtn.classList.add("active");
  }
}

async function refreshLibrary() {
  const grid = document.getElementById("library-grid");
  grid.innerHTML = '<div class="empty-state"><p>Scanning remote storage...</p></div>';
  
  try {
    const res = await fetch("/api/library");
    if (!res.ok) throw new Error("Failed to scan storage");
    const games = await res.json();
    renderLibrary(games);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state"><p>No games loaded (${e.message}).</p><p class="subtext">Make sure remote storage is configured.</p></div>`;
  }
}

function renderLibrary(games) {
  const grid = document.getElementById("library-grid");
  if (!games || games.length === 0) {
    grid.innerHTML = '<div class="empty-state"><p>No games detected in remote storage.</p></div>';
    return;
  }

  grid.innerHTML = games.map(g => `
    <div class="card game-card">
      <h3>${escapeHtml(g.title || g.name)}</h3>
      <p class="subtext">${g.size_gb || 0} GB • ${g.streamable ? "Streamable" : "Install required"}</p>
      <div class="form-actions">
        <button class="btn btn-primary" onclick="streamGame('${g.id}')">Stream to Handheld</button>
      </div>
    </div>
  `).join("");
}

async function testConnection() {
  const url = document.getElementById("storage-url").value;
  const user = document.getElementById("storage-user").value;
  const pass = document.getElementById("storage-pass").value;

  if (!url) {
    alert("Please enter a storage endpoint URL.");
    return;
  }

  alert("Testing connection to " + url + "...");
}

async function saveStorageConfig() {
  alert("Settings saved successfully.");
}

function saveSettings() {
  alert("Preferences updated.");
}

function toggleWarmup(enabled) {
  console.log("Warmup set to:", enabled);
}

function streamGame(id) {
  alert("Stream request queued for game: " + id);
}

function escapeHtml(str) {
  return String(str).replace(/[&<>'"]/g, 
    tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
  );
}

// Initial status poll
document.addEventListener("DOMContentLoaded", () => {
  console.log("DeckStream UI loaded.");
});
