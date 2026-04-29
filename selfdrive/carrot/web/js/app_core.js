const DEBUG_UI = false;

function disableViewportZoomGestures() {
  const preventGesture = (e) => e.preventDefault();

  ["gesturestart", "gesturechange", "gestureend"].forEach((type) => {
    document.addEventListener(type, preventGesture, { passive: false });
  });

  document.addEventListener("touchmove", (e) => {
    if (e.touches && e.touches.length > 1) e.preventDefault();
  }, { passive: false });

  let lastTouchEnd = 0;
  document.addEventListener("touchend", (e) => {
    const now = Date.now();
    if (now - lastTouchEnd <= 300) e.preventDefault();
    lastTouchEnd = now;
  }, { passive: false });
}

disableViewportZoomGestures();

let SETTINGS = null;
let CURRENT_GROUP = null;
let LANG = "ko";
const LANG_STORAGE_KEY = "carrot_web_lang";
const LANG_EMOJI = {
  ko: "🇰🇷",
  en: "🇺🇸",
  zh: "🇨🇳",
  ja: "🇯🇵",
  fr: "🇫🇷",
};



/* ── Action Labels (user-friendly status messages) ──────── */


/* ── Error Code → Friendly Message ────────────────────────── */


function friendlyError(json) {
  if (!json) return null;
  const code = json.error_code;
  const detail = json.error_detail || "";
  const langMap = ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en;
  if (code && langMap[code]) return langMap[code](detail);
  // fallback: try to make raw errors more readable
  const raw = json.error || "";
  if (raw.startsWith("git subcommand not allowed:")) {
    const sub = raw.replace("git subcommand not allowed:", "").trim();
    return (ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en).GIT_CMD_NOT_ALLOWED(sub);
  }
  if (raw.startsWith("not allowed:")) {
    const cmd = raw.replace("not allowed:", "").trim();
    return (ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en).CMD_NOT_ALLOWED(cmd);
  }
  if (raw === "timeout") return (ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en).CMD_TIMEOUT();
  if (raw === "bad mode") return (ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en).INVALID_RESET_MODE();
  if (raw === "missing branch") return (ERROR_MESSAGES[LANG] || ERROR_MESSAGES.en).MISSING_BRANCH();
  return raw || null;
}

function getActionLabel(action) {
  const labels = (ACTION_LABELS[LANG] || ACTION_LABELS.en)[action];
  return labels || { running: action + "...", done: action, failed: action };
}



let UNIT_CYCLE = [1, 2, 5, 10, 50, 100];
const UNIT_INDEX = {}; // per name

// Car select data
let CARS = null;                 // { makers: {Hyundai:[...], Genesis:[...]} }
let CURRENT_MAKER = null;

const btnHome = document.getElementById("btnHome");
const btnSetting = document.getElementById("btnSetting");
const btnLogs = document.getElementById("btnLogs");
const btnTerminal = document.getElementById("btnTerminal");
const btnLang = document.getElementById("btnLang");
const langLabel = document.getElementById("langLabel");
const btnSettingLang = document.getElementById("btnSettingLang");
const btnQuickLinkWeb = document.getElementById("btnQuickLinkWeb");
const btnTools = document.getElementById("btnTools");
const btnRecordToggle = document.getElementById("btnRecordToggle");
const btnSettingSearch = document.getElementById("btnSettingSearch");
const settingSearchBackdrop = document.getElementById("settingSearchBackdrop");
const settingSearchPanel = document.getElementById("settingSearchPanel");
const settingSearchTitle = document.getElementById("settingSearchTitle");
const settingSearchForm = document.getElementById("settingSearchForm");
const settingSearchInput = document.getElementById("settingSearchInput");
const btnSettingSearchSubmit = document.getElementById("btnSettingSearchSubmit");
const settingSearchMeta = document.getElementById("settingSearchMeta");
const settingSearchResults = document.getElementById("settingSearchResults");
const appToastHost = document.getElementById("appToastHost");
const appDialog = document.getElementById("appDialog");
const appDialogBackdrop = document.getElementById("appDialogBackdrop");
const appDialogTitle = document.getElementById("appDialogTitle");
const appDialogBody = document.getElementById("appDialogBody");
const appDialogChoices = document.getElementById("appDialogChoices");
const appDialogInputWrap = document.getElementById("appDialogInputWrap");
const appDialogInput = document.getElementById("appDialogInput");
const appDialogCancel = document.getElementById("appDialogCancel");
const appDialogCopy = document.getElementById("appDialogCopy");
const appDialogConfirm = document.getElementById("appDialogConfirm");
const appBranchPicker = document.getElementById("appBranchPicker");
const appBranchPickerBackdrop = document.getElementById("appBranchPickerBackdrop");
const appBranchPickerTitle = document.getElementById("appBranchPickerTitle");
const appBranchPickerMeta = document.getElementById("appBranchPickerMeta");
const appBranchPickerList = document.getElementById("appBranchPickerList");
const appBranchPickerClose = document.getElementById("appBranchPickerClose");
const appCarPicker = document.getElementById("appCarPicker");
const appCarPickerBackdrop = document.getElementById("appCarPickerBackdrop");
const appCarPickerTitle = document.getElementById("appCarPickerTitle");
const appCarPickerMeta = document.getElementById("appCarPickerMeta");
const appCarPickerList = document.getElementById("appCarPickerList");
const appCarPickerClose = document.getElementById("appCarPickerClose");
const swipeContainer = document.getElementById("swipeContainer");
const PAGE_ELEMENTS = {
  setting: document.getElementById("pageSetting"),
  car: document.getElementById("pageCar"),
  tools: document.getElementById("pageTools"),
  logs: document.getElementById("pageLogs"),
  terminal: document.getElementById("pageTerminal"),
  branch: document.getElementById("pageBranch"),
  carrot: document.getElementById("pageCarrot"),
};

function normalizeLangCode(raw) {
  const value = String(raw || "").trim().toLowerCase();
  const packs = window.CarrotTranslations?.packs || {};
  if (packs[value]) return value;
  if (value.startsWith("ko")) return "ko";
  if (value.startsWith("zh")) return "zh";
  if (value.startsWith("ja")) return "ja";
  if (value.startsWith("fr")) return "fr";
  if (value.startsWith("en")) return "en";
  return "";
}

function detectDefaultLang() {
  try {
    const stored = normalizeLangCode(localStorage.getItem(LANG_STORAGE_KEY));
    if (stored) return stored;
  } catch {}

  const browserLangs = Array.isArray(navigator.languages) && navigator.languages.length
    ? navigator.languages
    : [navigator.language, navigator.userLanguage];
  for (const candidate of browserLangs) {
    const normalized = normalizeLangCode(candidate);
    if (normalized) return normalized;
  }
  return "ko";
}

LANG = detectDefaultLang();
const TRANSLATION_REGISTRY = window.CarrotTranslations || { packs: {}, order: ["ko", "en", "zh"] };
const UI_STRINGS = TRANSLATION_REGISTRY.strings || {};
const ACTION_LABELS = TRANSLATION_REGISTRY.actionLabels || {};
const ERROR_MESSAGES = TRANSLATION_REGISTRY.errorMessages || {};
const DRIVE_MODES = TRANSLATION_REGISTRY.driveModes || {};

const PAGE_TRANSITION_CLASSES = [
  "page-transitioning",
  "page-active",
  "page-enter-from-right",
  "page-enter-from-left",
  "page-exit-to-left",
  "page-exit-to-right",
];
const PAGE_TRANSITION_MS = 280;
const SWIPE_SETTLE_MS = 220;
const SWIPE_COMMIT_RATIO = 0.22;
const SWIPE_VELOCITY_THRESHOLD = 0.45;
const SWIPE_EDGE_RESISTANCE = 0.18;
let pageTransitionTimer = null;
let pageTransitionToken = 0;
let CURRENT_PAGE = "carrot";
let appToastSerial = 0;
let activeAppToast = null;
let appToastHideTimer = null;
let appToastRemoveTimer = null;
let activeAppDialog = null;
let appDialogSerial = 0;
let settingScreenHideTimer = null;
let settingScreenTransitionToken = 0;
let carScreenHideTimer = null;
let carScreenTransitionToken = 0;

btnTools.onclick = () => showPage("tools", true, getSwipeTransition(CURRENT_PAGE, "tools"));

const curCarLabelCar = document.getElementById("curCarLabelCar");
const curCarLabelSetting = document.getElementById("curCarLabelSetting");

// Setting screens
const settingTitle = document.getElementById("settingTitle");
const btnBackGroups = document.getElementById("btnBackGroups");
const settingCarRow = document.getElementById("settingCarRow");
const settingScreenHost = document.getElementById("settingScreenHost");
const screenGroups = document.getElementById("settingScreenGroups");
const screenItems = document.getElementById("settingScreenItems");
const settingSubnavWrap = document.getElementById("settingSubnavWrap");
const settingSubnav = document.getElementById("settingSubnav");
const itemsTitle = document.getElementById("itemsTitle");

// Car screens
const carTitle = document.getElementById("carTitle");
const btnBackCar = document.getElementById("btnBackCar");
const carMeta = document.getElementById("carMeta");
const carScreenMakers = document.getElementById("carScreenMakers");
const carScreenModels = document.getElementById("carScreenModels");
const makerList = document.getElementById("makerList");
const modelList = document.getElementById("modelList");
const modelTitle = document.getElementById("modelTitle");
const modelMeta = document.getElementById("modelMeta");

btnHome.onclick = () => showPage("carrot", true, getSwipeTransition(CURRENT_PAGE, "carrot"));
btnRecordToggle.onclick = () => toggleRecord();
btnSetting.onclick = () => showPage("setting", true, getSwipeTransition(CURRENT_PAGE, "setting"));
if (btnLogs) btnLogs.onclick = () => showPage("logs", true, getSwipeTransition(CURRENT_PAGE, "logs"));
btnTerminal.onclick = () => showPage("terminal", true, getSwipeTransition(CURRENT_PAGE, "terminal"));

if (btnLang) btnLang.onclick = () => toggleLang();
if (btnSettingLang) btnSettingLang.onclick = () => toggleLang();

if (settingCarRow) {
  settingCarRow.onclick = () => {
    if (typeof window.openCarPickerFlow === "function") window.openCarPickerFlow();
    else showPage("car", true);
  };
  settingCarRow.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (typeof window.openCarPickerFlow === "function") window.openCarPickerFlow();
      else showPage("car", true);
    }
  };
}
btnBackCar.onclick = () => history.back();
carTitle.onclick = () => history.back();
modelTitle.onclick = () => showCarScreen("makers");

// Branch select
let BRANCHES = [];
let CURRENT_BRANCH_NAME = "";
const branchTitle = document.getElementById("branchTitle");
const btnBackBranch = document.getElementById("btnBackBranch");
const branchMeta = document.getElementById("branchMeta");
const branchList = document.getElementById("branchList");

// Quick Link
const quickLink = document.getElementById("toolsQuickLink");
const chipQuickLabel = document.getElementById("toolsQuickLinkTitle");
const btnSaveQuickLink = document.getElementById("btnToolsQuickLink");
const QUICK_LINK_FIXED_URL = "https://man.carrotpilot.app/";
let QUICK_LINK_URL = QUICK_LINK_FIXED_URL;
let QUICK_LINK_STATUS = "loading";
let QUICK_LINK_MESSAGE = "";
let quickLinkLoadPromise = null;
let quickLinkLoadedAt = 0;
let quickLinkActionTimer = null;

btnBackBranch.onclick = () => history.back();
branchTitle.onclick = () => history.back();

function clearPageTransitionClasses(el) {
  if (!el) return;
  el.classList.remove(...PAGE_TRANSITION_CLASSES);
}

function resetPageRuntimeStyles(el) {
  if (!el) return;
  el.style.transition = "";
  el.style.transform = "";
  el.style.opacity = "";
  el.style.zIndex = "";
  el.style.willChange = "";
  el.style.position = "";
  el.style.top = "";
  el.style.left = "";
  el.style.width = "";
}

function setDisplayedPage(page) {
  Object.entries(PAGE_ELEMENTS).forEach(([name, el]) => {
    if (!el) return;
    clearPageTransitionClasses(el);
    resetPageRuntimeStyles(el);
    el.style.display = (name === page) ? "" : "none";
  });
  if (swipeContainer) swipeContainer.style.minHeight = "";
  if (settingScreenHost) settingScreenHost.style.minHeight = "";
}

function clearPendingScreenHide(timerRef) {
  if (timerRef) {
    window.clearTimeout(timerRef);
  }
  return null;
}

function getSwipeTransition(fromPage, toPage) {
  if (fromPage === "terminal" || toPage === "terminal") return null;
  const fromIdx = SWIPE_PAGES.indexOf(fromPage);
  const toIdx = SWIPE_PAGES.indexOf(toPage);
  if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return null;
  return toIdx > fromIdx ? "forward" : "backward";
}

function getSwipeViewportMetrics(host = swipeContainer) {
  if (!host) {
    return { host: null, top: 0, left: 0, width: window.innerWidth || 1 };
  }

  const styles = window.getComputedStyle(host);
  const paddingTop = parseFloat(styles.paddingTop) || 0;
  const paddingLeft = parseFloat(styles.paddingLeft) || 0;
  const paddingRight = parseFloat(styles.paddingRight) || 0;
  const width = Math.max((host.clientWidth || window.innerWidth || 1) - paddingLeft - paddingRight, 1);

  return { host, top: paddingTop, left: paddingLeft, width };
}

function pinSwipeLayer(el, metrics) {
  if (!el) return;
  el.style.top = `${metrics.top}px`;
  el.style.left = `${metrics.left}px`;
  el.style.width = `${metrics.width}px`;
}

function updateSwipeFrameHeight(frame) {
  if (!frame?.host) return;
  const heights = [frame.fromEl, frame.toEl]
    .filter(Boolean)
    .map((el) => el.offsetHeight || 0);
  const maxHeight = Math.max(...heights, 0);
  frame.host.style.minHeight = maxHeight > 0 ? `${maxHeight}px` : "";
}

function prepareSwipeFrame(host, fromEl, toEl = null) {
  if (!host || !fromEl) return null;

  const metrics = getSwipeViewportMetrics(host);
  fromEl.style.display = "";
  fromEl.classList.add("page-transitioning", "page-active");
  fromEl.style.transition = "none";
  fromEl.style.willChange = "transform, opacity";
  pinSwipeLayer(fromEl, metrics);

  if (toEl) {
    toEl.style.display = "";
    toEl.classList.add("page-transitioning");
    toEl.style.transition = "none";
    toEl.style.willChange = "transform, opacity";
    pinSwipeLayer(toEl, metrics);
  }

  const frame = { host, fromEl, toEl, metrics, width: metrics.width };
  updateSwipeFrameHeight(frame);
  return frame;
}

function animatePageTransition(fromPage, toPage, transition) {
  const fromEl = PAGE_ELEMENTS[fromPage];
  const toEl = PAGE_ELEMENTS[toPage];
  if (!swipeContainer || !fromEl || !toEl || fromEl === toEl || !transition) {
    setDisplayedPage(toPage);
    return;
  }

  pageTransitionToken += 1;
  const token = pageTransitionToken;

  if (pageTransitionTimer) {
    clearTimeout(pageTransitionTimer);
    pageTransitionTimer = null;
  }

  Object.values(PAGE_ELEMENTS).forEach((el) => {
    if (!el) return;
    clearPageTransitionClasses(el);
    resetPageRuntimeStyles(el);
    if (el !== fromEl && el !== toEl) el.style.display = "none";
  });

  const frame = prepareSwipeFrame(swipeContainer, fromEl, toEl);
  if (!frame) {
    setDisplayedPage(toPage);
    return;
  }

  toEl.classList.add(transition === "forward" ? "page-enter-from-right" : "page-enter-from-left");

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (token !== pageTransitionToken) return;
      toEl.classList.add("page-active");
      toEl.classList.remove(
        transition === "forward" ? "page-enter-from-right" : "page-enter-from-left",
      );
      fromEl.classList.add(
        transition === "forward" ? "page-exit-to-left" : "page-exit-to-right",
      );
    });
  });

  pageTransitionTimer = setTimeout(() => {
    if (token !== pageTransitionToken) return;
    setDisplayedPage(toPage);
    pageTransitionTimer = null;
  }, PAGE_TRANSITION_MS);
}

function stopPageTransition() {
  if (pageTransitionTimer) {
    clearTimeout(pageTransitionTimer);
    pageTransitionTimer = null;
  }
  pageTransitionToken += 1;
  setDisplayedPage(CURRENT_PAGE);
}

function prepareSwipePages(fromPage, toPage) {
  const fromEl = PAGE_ELEMENTS[fromPage];
  const toEl = PAGE_ELEMENTS[toPage];
  if (!swipeContainer || !fromEl || !toEl) return null;

  stopPageTransition();

  Object.values(PAGE_ELEMENTS).forEach((el) => {
    if (!el) return;
    clearPageTransitionClasses(el);
    resetPageRuntimeStyles(el);
    if (el !== fromEl && el !== toEl) el.style.display = "none";
  });

  return prepareSwipeFrame(swipeContainer, fromEl, toEl);
}

function applySwipeDrag(frame, dx, direction, withResistance = false) {
  if (!frame) return;
  const { fromEl, toEl, width } = frame;
  const dragX = withResistance ? dx * SWIPE_EDGE_RESISTANCE : dx;
  const progress = Math.min(Math.abs(dragX) / width, 1);
  const targetBase = direction === "forward" ? width : -width;

  fromEl.style.transform = `translateX(${dragX}px)`;
  fromEl.style.opacity = `${1 - (progress * 0.14)}`;

  if (toEl) {
    toEl.style.transform = `translateX(${targetBase + dragX}px)`;
    toEl.style.opacity = `${0.82 + (progress * 0.18)}`;
    toEl.style.zIndex = "2";
  }

  updateSwipeFrameHeight(frame);
}

function settleSwipe(frame, direction, commit, done) {
  if (!frame) {
    done();
    return;
  }
  const { fromEl, toEl, width } = frame;
  const outX = direction === "forward" ? -width : width;
  const inX = direction === "forward" ? width : -width;
  const transition = `transform ${SWIPE_SETTLE_MS}ms cubic-bezier(0.22, 1, 0.36, 1), opacity ${SWIPE_SETTLE_MS}ms ease`;

  fromEl.style.transition = transition;
  if (toEl) toEl.style.transition = transition;

  requestAnimationFrame(() => {
    fromEl.style.transform = commit ? `translateX(${outX}px)` : "translateX(0px)";
    fromEl.style.opacity = commit ? "0" : "1";

    if (toEl) {
      toEl.style.transform = commit ? "translateX(0px)" : `translateX(${inX}px)`;
      toEl.style.opacity = commit ? "1" : "0.82";
    }
  });

  window.setTimeout(done, SWIPE_SETTLE_MS);
}

function showPage(page, pushHistory = false, transition = null) {
  const prevPage = CURRENT_PAGE;
  if (prevPage === "terminal" && page !== "terminal" && typeof teardownTerminalPage === "function") {
    teardownTerminalPage();
  }
  CURRENT_PAGE = page;
  document.documentElement.dataset.page = page;
  document.body.dataset.page = page;

  btnHome.classList.toggle("active", page === "carrot");
  btnSetting.classList.toggle("active", page === "setting");
  btnTools.classList.toggle("active", page === "tools");
  if (btnLogs) btnLogs.classList.toggle("active", page === "logs");
  btnTerminal.classList.toggle("active", page === "terminal");

  if (typeof updateAppViewportMetrics === "function") {
    updateAppViewportMetrics();
  }

  if (page === "setting" && SETTINGS) {
    if (typeof syncSettingViewportLayout === "function") {
      syncSettingViewportLayout().catch(() => {});
    } else if (pushHistory || !CURRENT_GROUP) {
      showSettingScreen("groups", false);
    }
  }

  if (page === "carrot" && window.HomeDrive && typeof window.HomeDrive.refresh === "function") {
    window.HomeDrive.refresh();
  }

  if (transition && prevPage !== page) animatePageTransition(prevPage, page, transition);
  else setDisplayedPage(page);

  window.dispatchEvent(new CustomEvent("carrot:pagechange", { detail: { page, prevPage } }));

  if (page !== "setting" && typeof closeSettingSearchPanel === "function") {
    closeSettingSearchPanel({ clear: false });
  }

  // Terminal uses its own fixed viewport layout. Resetting the window scroll
  // while entering/leaving it causes visible jumps on mobile.
  if (prevPage !== "terminal" && page !== "terminal") {
    window.scrollTo(0, 0);
  }

  if (page === "setting") {
    if (!SETTINGS) loadSettings();
    else if (typeof syncSettingViewportLayout === "function" && typeof isCompactLandscapeMode === "function" && isCompactLandscapeMode()) {
      syncSettingViewportLayout().catch(() => {});
    } else if (pushHistory || !CURRENT_GROUP) showSettingScreen("groups", false);
    loadCurrentCar().catch(() => {});
  }

  if (page === "car") {
    showCarScreen("makers", false);
    if (!CARS) loadCars();
    loadCurrentCar().catch(() => {});
  }
  if (page === "tools") {
    initToolsPage();
    updateQuickLink().catch(() => {});
  }
  if (page === "logs" && typeof initLogsPage === "function") {
    initLogsPage();
  }
  if (page === "terminal" && typeof initTerminalPage === "function") {
    initTerminalPage();
  }
  if (page === "carrot") {
    loadRecordState().catch(() => {});
  }

  const state =
    (page === "setting") ? { page: "setting", screen: "groups", group: null } :
    (page === "car") ? { page: "car", screen: "makers", maker: null } :
    (page === "tools") ? { page: "tools" } :
    (page === "logs") ? { page: "logs" } :
    (page === "terminal") ? { page: "terminal" } :
    (page === "carrot") ? { page: "carrot" } :
    (page === "branch") ? { page: "branch" } :
    { page: "carrot" };

  if (pushHistory) history.pushState(state, "");
  else history.replaceState(state, "");
}

/* ---------- screen transitions (Setting) ---------- */
function showSettingScreen(which, pushHistory = false) {
  const isGroups = (which === "groups");
  const showEl = isGroups ? screenGroups : screenItems;
  const hideEl = isGroups ? screenItems : screenGroups;
  const currentGroupLabel = (!isGroups && CURRENT_GROUP && typeof getSettingGroupLabel === "function")
    ? getSettingGroupLabel(CURRENT_GROUP)
    : (CURRENT_GROUP || "");
  const splitLandscape = (CURRENT_PAGE === "setting" && typeof isCompactLandscapeMode === "function" && isCompactLandscapeMode());
  const transitionToken = ++settingScreenTransitionToken;

  settingScreenHideTimer = clearPendingScreenHide(settingScreenHideTimer);

  if (splitLandscape) {
    settingTitle.textContent = UI_STRINGS[LANG].setting || "Setting";
    if (settingSubnavWrap) settingSubnavWrap.style.display = "none";
    if (showEl) {
      showEl.style.display = "";
      showEl.classList.remove("hidden");
    }
    if (hideEl) {
      hideEl.style.display = "";
      hideEl.classList.remove("hidden");
    }
    if (pushHistory) {
      history.replaceState({ page: "setting", screen: "items", group: CURRENT_GROUP || null }, "");
    }
    if (settingScreenHost) settingScreenHost.style.minHeight = "";
    return;
  }

  if (btnBackGroups) btnBackGroups.style.display = "none";
  settingTitle.textContent = isGroups ? (UI_STRINGS[LANG].setting || "Setting") : ((UI_STRINGS[LANG].setting || "Setting") + " - " + currentGroupLabel);
  if (settingSubnavWrap) settingSubnavWrap.style.display = isGroups ? "none" : "";

  showEl.style.display = "";
  requestAnimationFrame(() => {
    if (transitionToken !== settingScreenTransitionToken) return;
    showEl.classList.remove("hidden");
  });

  hideEl.classList.add("hidden");
  settingScreenHideTimer = window.setTimeout(() => {
    if (transitionToken !== settingScreenTransitionToken) return;
    hideEl.style.display = "none";
    settingScreenHideTimer = null;
  }, 170);

  if (pushHistory) {
    history.pushState({ page: "setting", screen: which, group: CURRENT_GROUP || null }, "");
  }

  if (settingScreenHost) settingScreenHost.style.minHeight = "";
  if (isGroups && typeof setSettingItemsScrollTop === "function") {
    requestAnimationFrame(() => setSettingItemsScrollTop(0));
  }
}

if (btnBackGroups) btnBackGroups.onclick = () => history.back();
settingTitle.onclick = () => history.back();
if (itemsTitle) itemsTitle.onclick = () => history.back();

/* ---------- screen transitions (Car) ---------- */
function showCarScreen(which, pushHistory = false) {
  const isMakers = (which === "makers");
  const showEl = isMakers ? carScreenMakers : carScreenModels;
  const hideEl = isMakers ? carScreenModels : carScreenMakers;
  const transitionToken = ++carScreenTransitionToken;

  carScreenHideTimer = clearPendingScreenHide(carScreenHideTimer);

  showEl.style.display = "";
  requestAnimationFrame(() => {
    if (transitionToken !== carScreenTransitionToken) return;
    showEl.classList.remove("hidden");
  });

  hideEl.classList.add("hidden");
  carScreenHideTimer = window.setTimeout(() => {
    if (transitionToken !== carScreenTransitionToken) return;
    hideEl.style.display = "none";
    carScreenHideTimer = null;
  }, 170);

  if (pushHistory) {
    history.pushState({ page: "car", screen: which, maker: CURRENT_MAKER || null }, "");
  }
}

function setWebLanguage(lang) {
  const normalized = normalizeLangCode(lang);
  if (!normalized || !UI_STRINGS[normalized]) return false;
  LANG = normalized;
  try {
    localStorage.setItem(LANG_STORAGE_KEY, LANG);
  } catch {}

  updateLangLabel();

  // Update static UI text
  renderUIText();
  loadRecordState().catch(() => {});
  if (typeof rerenderPageLangUi === "function") rerenderPageLangUi();

  if (SETTINGS) {
    if (typeof rebuildSettingSearchEntries === "function") rebuildSettingSearchEntries();
    renderGroups();
    if (typeof renderSettingSubnav === "function") renderSettingSubnav();
    if (CURRENT_GROUP) {
      const currentTop = typeof getSettingItemsScrollTop === "function"
        ? getSettingItemsScrollTop()
        : 0;
      renderItems(CURRENT_GROUP, { scrollMode: "restore", scrollTop: currentTop });
    }
  }
  window.dispatchEvent(new CustomEvent("carrot:languagechange", { detail: { lang: LANG } }));
  return true;
}

function toggleLang() {
  const order = (TRANSLATION_REGISTRY.order || ["ko", "en", "zh"]).filter((lang) => UI_STRINGS[lang]);
  const currentIndex = Math.max(0, order.indexOf(LANG));
  const next = order[(currentIndex + 1) % order.length] || "ko";
  setWebLanguage(next);
}

function renderUIText() {
  const s = UI_STRINGS[LANG];
  if (!s) return;
  document.title = "CarrotPilot";

  // Nav bar (nested spans — set last child text)
  setNavText("btnHome", s.home);
  setNavText("btnSetting", s.setting);
  setNavText("btnTools", s.tools);
  setNavText("btnLogs", s.logs);
  setNavText("btnTerminal", s.terminal);
  setText("btnQuickLinkWeb", "CarrotMan");

  setText("carrotTitle", "CarrotPilot");

  // Car Select
  setText("carTitle", s.car_select);
  setText("btnBackCar", s.back);
  setText("makersTitle", s.makers);
  setText("modelTitle", s.models);

  // Setting
  setText("settingTitleText", s.setting);
  setText("settingCarEyebrow", s.car_select);
  setText("btnBackGroups", s.back);
  setText("groupsTitle", s.groups);
  setText("itemsTitle", s.items);

  // Tools
  setText("toolsTitle", s.tools);
  setText("gitCommandsTitle", s.git_commands);
  setText("userSystemTitle", s.user_system);
  setText("toolsQuickLinkTitle", "Link");
  setText("userSettingsTitle", s.section_settings_backup);
  setText("btnDeviceInfo", s.device_info || "Device Info");
  setText("btnGitRemote", s.change_repository || "change repository");
  setText("btnGitBranch", s.change_branch || "change branch");
  setText("btnGitAddRemote", s.add_remote || "add remote");
  setText("btnGitResetRepo", s.reset_repo || "reset repo");
  setText("btnDeviceLang", s.device_lang || "Device Lang");
  setText("btnResetCalib", s.reset_calib || "Reset Calib");
  setText("btnSendTmuxLog", s.capture_tmux || "capture tmux");
  setText("btnSendTmuxServerLog", s.send_tmux || "send tmux");
  setText("btnInstallRequired", s.install_required || "install flask");
  setText("btnDeleteVideos", s.delete_all_videos || "delete all videos");
  setText("btnDeleteLogs", s.delete_all_logs || "delete all logs");
  setText("btnRebuildAll", s.rebuild_all || "Rebuild All");
  setText("btnReboot", s.reboot);
  setText("btnBackupSettings", s.backup);
  setText("btnRestoreSettings", s.restore);
  setText("btnCopySettings", s.copy || "Copy");
  setText("btnViewSettings", s.view || "View");
  setText("sysCmdTitle", s.section_sys_cmd);
  setText("sysCmdHelp", s.sys_cmd_help);
  setText("outputTitle", s.section_output);
  setText("terminalTitle", s.terminal);
  setText("terminalSessionMeta", "/data/openpilot");
  setText("btnTerminalCtrlC", s.terminal_ctrl_c);
  setText("btnTerminalClear", s.terminal_clear);
  setText("btnTerminalReconnect", s.terminal_reconnect);
  setText("btnTerminalSend", s.terminal_send);
  setText("logsDashcamTitle", s.logs_dashcam || "Dashcam");
  setText("logsScreenTitle", s.logs_screenrecord || "Screen Record");
  setText("btnStartVision", `▶ ${s.start_vision || "Start Drive Vision"}`);
  const terminalInput = document.getElementById("terminalInput");
  if (terminalInput) terminalInput.placeholder = "";
  setText("settingSearchTitle", s.setting_search);
  if (settingSearchInput) settingSearchInput.placeholder = s.setting_search_placeholder || "";
  if (settingSearchMeta && (!settingSearchInput || !settingSearchInput.value.trim())) {
    settingSearchMeta.textContent = s.setting_search_idle || "";
  }
  if (btnSettingSearch) {
    btnSettingSearch.setAttribute("aria-label", s.setting_search || "Search Settings");
    btnSettingSearch.title = s.setting_search || "Search Settings";
  }
  if (btnSettingSearchSubmit) {
    btnSettingSearchSubmit.setAttribute("aria-label", s.setting_search || "Search Settings");
    btnSettingSearchSubmit.title = s.setting_search || "Search Settings";
  }
  if (typeof renderSettingSearchResults === "function" && settingSearchPanel && !settingSearchPanel.hidden) {
    renderSettingSearchResults(settingSearchInput?.value || "");
  }
  setText("appBranchPickerTitle", s.branch_select);
  setText("appBranchPickerClose", s.close);
  setText("appCarPickerTitle", s.car_select);
  setText("appCarPickerClose", s.cancel);
  updateLangLabel();
  syncHomeUtilityButtons();
  if (window.DrivingHud && typeof window.DrivingHud.renderText === "function") {
    window.DrivingHud.renderText();
  }
  renderQuickLinkUI();
}

function setNavText(id, txt) {
  const el = document.getElementById(id);
  if (!el) return;
  // The label is the last <span> child
  const spans = el.querySelectorAll(":scope > span");
  if (spans.length >= 2) spans[spans.length - 1].textContent = txt;
  else el.textContent = txt;
}

function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}

function updateLangLabel() {
  const main = langLabel?.querySelector(".lang-label__main");
  const sub = langLabel?.querySelector(".lang-label__sub");
  const emoji = LANG_EMOJI[LANG] || "🌐";
  const pack = TRANSLATION_REGISTRY.getPack?.(LANG) || {};
  const languageName = pack.name || LANG.toUpperCase();
  const nativeName = pack.nativeName || languageName;
  if (langLabel) {
    if (main && sub) {
      main.textContent = emoji;
      sub.textContent = "";
      sub.hidden = true;
    } else {
      langLabel.textContent = emoji;
    }
  }

  if (btnLang) {
    const text = `${getUIText("language", getUIText("lang", "Language"))} (${languageName})`;
    btnLang.setAttribute("aria-label", text);
    btnLang.title = text;
  }
  if (btnSettingLang) {
    const label = `${getUIText("language", getUIText("lang", "Language"))} · ${nativeName}`;
    btnSettingLang.textContent = label;
    btnSettingLang.title = label;
  }
  document.documentElement.lang = LANG;
}

function formatUIText(text, vars = {}) {
  let out = String(text ?? "");
  Object.entries(vars || {}).forEach(([key, value]) => {
    out = out.replaceAll(`{${key}}`, String(value));
  });
  return out;
}

function getUIText(key, fallback = "", vars = null) {
  const value = UI_STRINGS[LANG]?.[key] ?? UI_STRINGS.en?.[key] ?? UI_STRINGS.ko?.[key] ?? fallback;
  return vars ? formatUIText(value, vars) : value;
}

function syncModalBodyLock() {
  const hasOpenDialog =
    Boolean(appDialog && !appDialog.hidden) ||
    Boolean(appBranchPicker && !appBranchPicker.hidden) ||
    Boolean(appCarPicker && !appCarPicker.hidden) ||
    Boolean(settingSearchPanel && !settingSearchPanel.hidden);
  document.body.classList.toggle("dialog-open", hasOpenDialog);
}

function showAppToast(message, opts = {}) {
  if (!appToastHost || !message) return;

  const tone = opts.tone || "default";
  const duration = opts.duration ?? 2600;
  let toast = activeAppToast;
  if (!toast || !toast.isConnected) {
    toast = document.createElement("div");
    appToastHost.innerHTML = "";
    appToastHost.appendChild(toast);
    activeAppToast = toast;
  }

  toast.className = "app-toast";
  if (tone && tone !== "default") toast.classList.add(`is-${tone}`);
  toast.textContent = String(message);

  if (appToastHideTimer) {
    clearTimeout(appToastHideTimer);
    appToastHideTimer = null;
  }
  if (appToastRemoveTimer) {
    clearTimeout(appToastRemoveTimer);
    appToastRemoveTimer = null;
  }

  appToastSerial += 1;
  const toastSerial = appToastSerial;
  requestAnimationFrame(() => {
    if (!activeAppToast || toastSerial !== appToastSerial) return;
    activeAppToast.classList.add("is-visible");
  });

  appToastHideTimer = window.setTimeout(() => {
    if (!activeAppToast || toastSerial !== appToastSerial) return;
    activeAppToast.classList.remove("is-visible");
    appToastHideTimer = null;
    appToastRemoveTimer = window.setTimeout(() => {
      if (!activeAppToast || toastSerial !== appToastSerial) return;
      activeAppToast.remove();
      activeAppToast = null;
      appToastRemoveTimer = null;
    }, 180);
  }, duration);
}

function resolveAppDialog(result) {
  if (!activeAppDialog || !appDialog) return;

  const state = activeAppDialog;
  activeAppDialog = null;
  const dialogSerial = state.serial;
  appDialog.classList.remove("is-open");

  window.setTimeout(() => {
    if (dialogSerial !== appDialogSerial) {
      state.resolve(result);
      return;
    }
    appDialog.hidden = true;
    syncModalBodyLock();
    if (appDialogChoices) {
      appDialogChoices.hidden = true;
      appDialogChoices.innerHTML = "";
    }
    if (appDialogInputWrap) appDialogInputWrap.hidden = true;
    if (appDialogInput) {
      appDialogInput.value = "";
      appDialogInput.placeholder = "";
    }
    if (state.lastFocus && typeof state.lastFocus.focus === "function") {
      state.lastFocus.focus();
    }
    state.resolve(result);
  }, 180);
}

function cancelAppDialog() {
  if (!activeAppDialog) return;
  const result = activeAppDialog.mode === "prompt" || activeAppDialog.mode === "choice"
    ? null
    : false;
  resolveAppDialog(result);
}

function confirmAppDialog() {
  if (!activeAppDialog) return;
  const result = activeAppDialog.mode === "prompt"
    ? (appDialogInput ? appDialogInput.value : "")
    : true;
  resolveAppDialog(result);
}

function openAppDialog(options = {}) {
  if (!appDialog || !appDialogTitle || !appDialogBody || !appDialogConfirm || !appDialogCancel) {
    if (options.mode === "prompt") return Promise.resolve(null);
    return Promise.resolve(options.mode === "alert");
  }

  if (activeAppDialog) cancelAppDialog();

  const mode = options.mode || "alert";
  const title =
    options.title ||
    (mode === "confirm"
      ? getUIText("confirm_title", "Confirm")
      : mode === "prompt"
        ? getUIText("input_title", "Input")
        : getUIText("notice", "Notice"));
  const message = options.message || "";
  const messageHtml = options.messageHtml || "";
  const useHtml = Boolean(options.html);
  const confirmLabel = options.confirmLabel || getUIText("ok", "OK");
  const cancelLabel = options.cancelLabel || getUIText("cancel", "Cancel");
  const choices = Array.isArray(options.choices)
    ? options.choices.filter((choice) => choice && (choice.label || choice.labelHtml))
    : [];
  const isChoice = mode === "choice" || choices.length > 0;
  const showCancel = mode !== "alert";

  appDialogTitle.textContent = title;
  if (useHtml) appDialogBody.innerHTML = String(messageHtml || message);
  else appDialogBody.textContent = String(message);
  // When choices exist, body should not grow (just show message); otherwise body scrolls fully
  appDialogBody.style.flex = isChoice ? "0 0 auto" : "1 1 auto";
  appDialogConfirm.textContent = confirmLabel;
  appDialogCancel.textContent = cancelLabel;
  appDialogCancel.hidden = !showCancel;
  appDialogCancel.setAttribute("aria-hidden", showCancel ? "false" : "true");
  appDialogConfirm.hidden = isChoice;
  appDialogConfirm.setAttribute("aria-hidden", isChoice ? "true" : "false");

  const copyText = options.copyText || "";
  if (appDialogCopy) {
    appDialogCopy.hidden = !copyText;
    appDialogCopy.textContent = LANG === "en" ? "Copy" : LANG === "zh" ? "复制" : "복사";
    appDialogCopy.onclick = copyText ? () => {
      copyToClipboard(copyText);
      alert(LANG === "ko" ? "복사되었습니다" : LANG === "zh" ? "已复制" : "Copied");
    } : null;
  }

  if (appDialogChoices) {
    appDialogChoices.innerHTML = "";
    appDialogChoices.hidden = !isChoice;
    for (const choice of choices) {
      const button = document.createElement("button");
      button.type = "button";
      let btnClass = choice.danger
        ? "btn btn--danger app-dialog__choiceBtn"
        : "btn app-dialog__choiceBtn";
      if (choice.className) btnClass += " " + choice.className;
      button.className = btnClass;
      if (choice.labelHtml) {
        button.innerHTML = choice.labelHtml;
      } else {
        button.textContent = String(choice.label);
      }
      button.style.cssText = "text-align:left; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
      button.addEventListener("click", () => resolveAppDialog(choice.value));
      appDialogChoices.appendChild(button);
    }
  }

  if (appDialogInputWrap && appDialogInput) {
    const isPrompt = mode === "prompt";
    appDialogInputWrap.hidden = !isPrompt;
    appDialogInput.value = options.defaultValue ?? "";
    appDialogInput.placeholder = options.placeholder || "";
  }

  return new Promise((resolve) => {
    const dialogSerial = ++appDialogSerial;
    activeAppDialog = {
      resolve,
      mode,
      serial: dialogSerial,
      lastFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
    };

    appDialog.hidden = false;
    syncModalBodyLock();

    requestAnimationFrame(() => {
      appDialog.classList.add("is-open");
      if (mode === "prompt" && appDialogInput) {
        appDialogInput.focus();
        appDialogInput.select();
      } else if (isChoice && appDialogChoices) {
        const firstChoice = appDialogChoices.querySelector("button");
        if (firstChoice && typeof firstChoice.focus === "function") firstChoice.focus();
      } else {
        appDialogConfirm.focus();
      }
    });
  });
}

function appAlert(message, opts = {}) {
  return openAppDialog({
    mode: "alert",
    title: opts.title,
    message,
    messageHtml: opts.messageHtml,
    html: opts.html,
    confirmLabel: opts.confirmLabel,
    copyText: opts.copyText,
  });
}

function appConfirm(message, opts = {}) {
  return openAppDialog({
    mode: "confirm",
    title: opts.title,
    message,
    confirmLabel: opts.confirmLabel,
    cancelLabel: opts.cancelLabel,
  });
}

function appPrompt(message, opts = {}) {
  return openAppDialog({
    mode: "prompt",
    title: opts.title,
    message,
    confirmLabel: opts.confirmLabel,
    cancelLabel: opts.cancelLabel,
    defaultValue: opts.defaultValue,
    placeholder: opts.placeholder,
  });
}

if (appDialogBackdrop) appDialogBackdrop.onclick = cancelAppDialog;
if (appDialogCancel) appDialogCancel.onclick = cancelAppDialog;
if (appDialogConfirm) appDialogConfirm.onclick = confirmAppDialog;

document.addEventListener("keydown", (ev) => {
  if (!activeAppDialog) return;

  if (ev.key === "Escape") {
    ev.preventDefault();
    if (activeAppDialog.mode === "alert") resolveAppDialog(true);
    else cancelAppDialog();
    return;
  }

  if (ev.key === "Enter" && !ev.shiftKey) {
    const targetTag = ev.target?.tagName;
    if (targetTag === "TEXTAREA") return;
    ev.preventDefault();
    confirmAppDialog();
  }
});

function syncHomeUtilityButtons() {
  return;
}

function flashQuickLinkActionLabel(label, duration = 1400) {
  if (!btnSaveQuickLink) return;
  if (quickLinkActionTimer) clearTimeout(quickLinkActionTimer);
  btnSaveQuickLink.textContent = label;
  quickLinkActionTimer = window.setTimeout(() => {
    quickLinkActionTimer = null;
    syncHomeUtilityButtons();
  }, duration);
}

function renderQuickLinkUI() {
  const hasUrl = Boolean(QUICK_LINK_URL);
  const emptyMessage = QUICK_LINK_MESSAGE || getUIText("quick_link_empty", "GithubUsername not set");
  const loadingMessage = getUIText("connecting", "Connecting...");
  const errorMessage = QUICK_LINK_MESSAGE || getUIText("error", "Error");
  const inlineText = hasUrl
    ? QUICK_LINK_URL
    : (
      QUICK_LINK_STATUS === "loading"
        ? loadingMessage
        : (QUICK_LINK_STATUS === "error" ? errorMessage : emptyMessage)
    );

  if (quickLink) {
    if (hasUrl) {
      quickLink.href = QUICK_LINK_URL;
      quickLink.textContent = inlineText;
      quickLink.removeAttribute("aria-disabled");
    } else {
      quickLink.removeAttribute("href");
      quickLink.setAttribute("aria-disabled", "true");
      quickLink.textContent = inlineText;
    }
  }

  if (btnSaveQuickLink) {
    btnSaveQuickLink.disabled = !hasUrl;
    btnSaveQuickLink.setAttribute("aria-disabled", hasUrl ? "false" : "true");
  }

  if (btnQuickLinkWeb) {
    if (hasUrl) {
      btnQuickLinkWeb.href = QUICK_LINK_URL;
      btnQuickLinkWeb.setAttribute("aria-disabled", "false");
    } else {
      btnQuickLinkWeb.removeAttribute("href");
      btnQuickLinkWeb.setAttribute("aria-disabled", "true");
    }
  }
}

function setServerStateStatus() {}

async function updateQuickLink(options = {}) {
  const silent = options.silent === true;
  QUICK_LINK_URL = QUICK_LINK_FIXED_URL;
  QUICK_LINK_STATUS = "ready";
  QUICK_LINK_MESSAGE = "";
  quickLinkLoadPromise = null;
  quickLinkLoadedAt = Date.now();
  if (!silent || CURRENT_PAGE === "tools") renderQuickLinkUI();
  return QUICK_LINK_URL;
}

async function openQuickLink() {
  QUICK_LINK_URL = QUICK_LINK_FIXED_URL;
  renderQuickLinkUI();
  const msg = LANG === "ko"
    ? `CarrotMan을 여시겠습니까?\n\n${QUICK_LINK_FIXED_URL}`
    : `${getUIText("open", "Open")} CarrotMan?\n\n${QUICK_LINK_FIXED_URL}`;
  const ok = await appConfirm(msg, { title: "CarrotMan" });
  if (!ok) return;
  window.open(QUICK_LINK_FIXED_URL, "_blank", "noopener");
}

if (btnQuickLinkWeb) {
  btnQuickLinkWeb.onclick = (e) => {
    e.preventDefault();
    openQuickLink().catch(() => {});
  };
}

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => {});
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;top:-9999px;opacity:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch {}
  document.body.removeChild(ta);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatItemText(p, keyKo, keyEn, fallback = "") {
  if (LANG === "zh") return (p["c" + keyEn.slice(1)] || p[keyEn] || p[keyKo] || fallback);
  if (LANG === "ko") return (p[keyKo] ?? fallback);
  return (p[keyEn] ?? p[keyKo] ?? fallback);
}

function clamp(v, mn, mx) {
  if (Number.isFinite(mn) && v < mn) return mn;
  if (Number.isFinite(mx) && v > mx) return mx;
  return v;
}

/* ---------- Params helpers ---------- */
async function bulkGet(names) {
  const q = encodeURIComponent(names.join(","));
  const r = await fetch("/api/params_bulk?names=" + q);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "bulk failed");
  return j.values || {};
}

async function setParam(name, value) {
  const r = await fetch("/api/param_set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, value })
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "set failed");
  window.dispatchEvent(new CustomEvent("carrot:paramchange", {
    detail: { name, value: j.value ?? value },
  }));
  return true;
}

/* ── Swipe Navigation ──────────────────────────────────── */
const SWIPE_PAGES = ["carrot", "setting", "tools", "logs", "terminal"];
const SETTING_BACK_EDGE_WIDTH = 32;

function isLandscapeRailMode() {
  return window.matchMedia("(orientation: landscape)").matches;
}

function isSettingItemsScreenActive() {
  return Boolean(
    CURRENT_PAGE === "setting" &&
    screenItems &&
    screenItems.style.display !== "none" &&
    !screenItems.classList.contains("hidden")
  );
}

function prepareSettingBackFrame() {
  if (!settingScreenHost || !screenItems || !screenGroups) return null;
  if (typeof stopSettingSubnavMotion === "function") stopSettingSubnavMotion();

  [screenItems, screenGroups].forEach((el) => {
    clearPageTransitionClasses(el);
    resetPageRuntimeStyles(el);
    el.classList.remove("hidden");
  });

  const frame = prepareSwipeFrame(settingScreenHost, screenItems, screenGroups);
  if (!frame) return null;
  settingScreenHost.classList.add("setting-back-swiping");
  screenItems.style.zIndex = "2";
  screenGroups.style.zIndex = "1";
  return frame;
}

function cleanupSettingBackFrame() {
  if (!settingScreenHost || !screenItems || !screenGroups) return;
  settingScreenHost.style.minHeight = "";
  settingScreenHost.classList.remove("setting-back-swiping");
  [screenItems, screenGroups].forEach((el) => {
    clearPageTransitionClasses(el);
    resetPageRuntimeStyles(el);
  });
}

(function initSwipe() {
  const el = swipeContainer;
  if (!el) return;

  let gesture = null;

  el.addEventListener("touchstart", (e) => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    const inSettingItems = isSettingItemsScreenActive();
    if (
      e.touches.length !== 1 ||
      !SWIPE_PAGES.includes(CURRENT_PAGE) ||
      inSettingItems ||
      (inSettingItems && e.target?.closest?.("#settingSubnavWrap"))
    ) {
      gesture = null;
      return;
    }

    const touch = e.touches[0];
    gesture = {
      tracking: true,
      dragging: false,
      startX: touch.clientX,
      startY: touch.clientY,
      dx: 0,
      direction: null,
      targetPage: null,
      settingGroupTarget: null,
      settingBackTarget: false,
      frame: null,
      velocity: 0,
      lastX: touch.clientX,
      lastTime: performance.now(),
    };
  }, { passive: true });

  el.addEventListener("touchmove", (e) => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    const inSettingItems = isSettingItemsScreenActive();
    if (inSettingItems) {
      gesture = null;
      return;
    }
    if (!gesture?.tracking || e.touches.length !== 1) return;

    const touch = e.touches[0];
    const dx = touch.clientX - gesture.startX;
    const dy = touch.clientY - gesture.startY;

    if (!gesture.dragging) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      if (Math.abs(dy) > Math.abs(dx) * 0.9) {
        gesture = null;
        return;
      }

      const inSettingItems = isSettingItemsScreenActive();
      const direction = dx < 0 ? "forward" : "backward";
      const isSettingEdgeBack = inSettingItems && direction === "backward" && gesture.startX <= SETTING_BACK_EDGE_WIDTH;
      if (isSettingEdgeBack) {
        gesture = null;
        return;
      }

      const idx = SWIPE_PAGES.indexOf(CURRENT_PAGE);
      const nextSettingGroup = inSettingItems && typeof getSettingSubnavShiftTarget === "function"
        ? getSettingSubnavShiftTarget(direction)
        : null;
      const settingBackTarget = Boolean(inSettingItems && direction === "backward" && nextSettingGroup?.reachedEdge);
      const settingGroupTarget = (nextSettingGroup && !nextSettingGroup.reachedEdge)
        ? nextSettingGroup.group
        : null;
      const targetPage = settingGroupTarget
        ? null
        : (
          inSettingItems
            ? (direction === "forward" ? "tools" : null)
            : (direction === "forward" ? SWIPE_PAGES[idx + 1] : SWIPE_PAGES[idx - 1])
        );

      gesture.dragging = true;
      gesture.direction = direction;
      gesture.targetPage = targetPage || null;
      gesture.settingGroupTarget = settingGroupTarget || null;
      gesture.settingBackTarget = settingBackTarget;
      gesture.edgeResistance = !targetPage && !settingGroupTarget && !settingBackTarget;
      gesture.frame = targetPage
        ? prepareSwipePages(CURRENT_PAGE, targetPage)
        : (
          settingGroupTarget
            ? null
            : (
              settingBackTarget
                ? prepareSettingBackFrame()
                : (stopPageTransition(), prepareSwipeFrame(swipeContainer, PAGE_ELEMENTS[CURRENT_PAGE]))
            )
        );
    }

    if (!gesture.dragging) return;

    e.preventDefault();

    const constrainedDx =
      gesture.direction === "forward" ? Math.min(dx, 0) : Math.max(dx, 0);

    const now = performance.now();
    const dt = Math.max(now - gesture.lastTime, 1);
    gesture.velocity = (touch.clientX - gesture.lastX) / dt;
    gesture.lastX = touch.clientX;
    gesture.lastTime = now;
    gesture.dx = constrainedDx;

    if (gesture.frame) applySwipeDrag(gesture.frame, constrainedDx, gesture.direction, gesture.edgeResistance);
  }, { passive: false });

  el.addEventListener("touchend", (e) => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    const inSettingItems = isSettingItemsScreenActive();
    if (inSettingItems) {
      gesture = null;
      return;
    }
    if (!gesture) return;

    if (!gesture.dragging) {
      gesture = null;
      return;
    }

    const dx = gesture.dx;
    const width = gesture.frame?.width || getSwipeViewportMetrics(swipeContainer).width;
    const travel = Math.abs(dx) / width;
    const velocityOk =
      (gesture.direction === "forward" && gesture.velocity < -SWIPE_VELOCITY_THRESHOLD) ||
      (gesture.direction === "backward" && gesture.velocity > SWIPE_VELOCITY_THRESHOLD);
    const shouldCommitPage = Boolean(gesture.targetPage) && (travel > SWIPE_COMMIT_RATIO || velocityOk);
    const shouldCommitGroup = Boolean(gesture.settingGroupTarget) && (Math.abs(dx) > 48 || velocityOk);
    const shouldCommitBack = Boolean(gesture.settingBackTarget) && (travel > SWIPE_COMMIT_RATIO || velocityOk);

    if (gesture.frame && gesture.targetPage) {
      const targetPage = gesture.targetPage;
      const direction = gesture.direction;
      const frame = gesture.frame;
      gesture = null;
      settleSwipe(frame, direction, shouldCommitPage, () => {
        if (shouldCommitPage && targetPage) showPage(targetPage, true, null);
        else setDisplayedPage(CURRENT_PAGE);
      });
      return;
    }

    if (gesture.settingGroupTarget) {
      const groupTarget = gesture.settingGroupTarget;
      const direction = gesture.direction;
      gesture = null;
      if (shouldCommitGroup && typeof animateSettingGroupSwitch === "function") {
        animateSettingGroupSwitch(groupTarget, direction).catch((e) => console.log("[SettingSwipe] switch failed:", e));
      } else if (shouldCommitGroup && typeof selectGroup === "function") {
        selectGroup(groupTarget, false);
      } else if (typeof centerActiveSettingSubnavTab === "function") {
        centerActiveSettingSubnavTab("smooth");
      }
      return;
    }

    if (gesture.settingBackTarget && gesture.frame) {
      const frame = gesture.frame;
      gesture = null;
      settleSwipe(frame, "backward", shouldCommitBack, () => {
        cleanupSettingBackFrame();
        if (shouldCommitBack) history.back();
        else showSettingScreen("items", false);
      });
      return;
    }

    if (gesture.frame) {
      const frame = gesture.frame;
      const direction = gesture.direction;
      gesture = null;
      settleSwipe(frame, direction, false, () => setDisplayedPage(CURRENT_PAGE));
      return;
    }
    gesture = null;
  }, { passive: true });

  el.addEventListener("touchcancel", () => {
    if (!gesture) return;
    if (gesture.settingBackTarget && gesture.frame) {
      const frame = gesture.frame;
      gesture = null;
      settleSwipe(frame, "backward", false, () => {
        cleanupSettingBackFrame();
        showSettingScreen("items", false);
      });
      return;
    }
    if (gesture.frame) {
      const frame = gesture.frame;
      const direction = gesture.direction;
      gesture = null;
      settleSwipe(frame, direction, false, () => setDisplayedPage(CURRENT_PAGE));
      return;
    }
    setDisplayedPage(CURRENT_PAGE);
    gesture = null;
  }, { passive: true });
})();

(function initSettingBackSwipe() {
  const host = settingScreenHost;
  if (!host || !screenItems || !screenGroups) return;

  let gesture = null;

  host.addEventListener("touchstart", (e) => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    if (
      e.touches.length !== 1 ||
      !isSettingItemsScreenActive() ||
      e.target?.closest?.("#settingSubnav") ||
      e.touches[0].clientX > SETTING_BACK_EDGE_WIDTH
    ) {
      gesture = null;
      return;
    }

    const touch = e.touches[0];
    gesture = {
      dragging: false,
      startX: touch.clientX,
      startY: touch.clientY,
      dx: 0,
      velocity: 0,
      lastX: touch.clientX,
      lastTime: performance.now(),
      frame: null,
    };
  }, { passive: true });

  host.addEventListener("touchmove", (e) => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    if (!gesture || e.touches.length !== 1) return;

    const touch = e.touches[0];
    const dx = touch.clientX - gesture.startX;
    const dy = touch.clientY - gesture.startY;

    if (!gesture.dragging) {
      if (dx < 10 && Math.abs(dy) < 10) return;
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx) * 0.9) {
        gesture = null;
        return;
      }

      if (typeof stopSettingSubnavMotion === "function") stopSettingSubnavMotion();
      gesture.dragging = true;
      gesture.frame = prepareSettingBackFrame();
    }

    e.preventDefault();

    const constrainedDx = Math.max(dx, 0);
    const now = performance.now();
    const dt = Math.max(now - gesture.lastTime, 1);
    gesture.velocity = (touch.clientX - gesture.lastX) / dt;
    gesture.lastX = touch.clientX;
    gesture.lastTime = now;
    gesture.dx = constrainedDx;

    applySwipeDrag(gesture.frame, constrainedDx, "backward");
  }, { passive: false });

  host.addEventListener("touchend", () => {
    if (isLandscapeRailMode()) {
      gesture = null;
      return;
    }
    if (!gesture) return;
    if (!gesture.dragging || !gesture.frame) {
      gesture = null;
      return;
    }

    const travel = gesture.dx / gesture.frame.width;
    const shouldCommit = travel > SWIPE_COMMIT_RATIO || gesture.velocity > SWIPE_VELOCITY_THRESHOLD;
    const frame = gesture.frame;
    gesture = null;

    settleSwipe(frame, "backward", shouldCommit, () => {
      cleanupSettingBackFrame();
      if (shouldCommit) history.back();
      else showSettingScreen("items", false);
    });
  }, { passive: true });

  host.addEventListener("touchcancel", () => {
    if (!gesture) return;
    const frame = gesture.frame;
    gesture = null;

    if (!frame) {
      cleanupSettingBackFrame();
      showSettingScreen("items", false);
      return;
    }

    settleSwipe(frame, "backward", false, () => {
      cleanupSettingBackFrame();
      showSettingScreen("items", false);
    });
  }, { passive: true });
})();

syncHomeUtilityButtons();
renderQuickLinkUI();
