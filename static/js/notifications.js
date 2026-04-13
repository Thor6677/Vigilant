/* Vigilant — Browser notification handler */

(function() {
    var POLL_INTERVAL = 30000;
    var STORAGE_KEY = 'vigilant_notif_enabled';
    var EVENTS_KEY = 'vigilant_notif_events';
    var LOCK_KEY = 'vigilant_notif_lock';
    var PREFS_KEY = 'vigilant_notif_prefs';
    var pollTimer = null;
    var storedEvents = [];
    var dropdownOpen = false;
    var showHidden = false;
    var expandedFilterType = null; /* which filtered type is expanded */

    /* Default: all types enabled */
    var DEFAULT_PREFS = {
        skill_complete: true,
        job_ready: true,
        new_mail: true,
        pi_expiring: true,
        structure_attack: true,
        structure_fuel: true,
        structure_change: true,
        sovereignty: true,
        moonmining: true,
        poco: true,
        inventory_low: true,
        contract_low: true,
    };

    /* Human-readable type labels */
    var TYPE_LABELS = {
        skill_complete: 'Skill',
        job_ready: 'Industry',
        new_mail: 'Mail',
        pi_expiring: 'PI',
        structure_attack: 'Structure Attack',
        structure_fuel: 'Structure Fuel',
        structure_change: 'Structure',
        sovereignty: 'Sovereignty',
        moonmining: 'Moonmining',
        poco: 'POCO',
        inventory_low: 'Inventory',
        inventory_critical: 'Inventory',
        contract_low: 'Contracts',
        contract_critical: 'Contracts',
        structure_alert: 'Structure',
    };

    var TYPE_COLORS = {
        skill_complete: 'var(--success)',
        job_ready: 'var(--accent)',
        pi_expiring: 'var(--danger)',
        new_mail: 'var(--text)',
        structure_attack: 'var(--danger)',
        structure_fuel: 'var(--accent)',
        structure_change: 'var(--muted)',
        sovereignty: 'var(--warn, var(--accent))',
        moonmining: 'var(--accent)',
        poco: 'var(--danger)',
        structure_alert: 'var(--danger)',
        inventory_low: 'var(--accent)',
        inventory_critical: 'var(--danger)',
        contract_low: 'var(--accent)',
        contract_critical: 'var(--danger)',
    };

    function loadPrefs() {
        try {
            var raw = localStorage.getItem(PREFS_KEY);
            if (raw) {
                var prefs = JSON.parse(raw);
                for (var k in DEFAULT_PREFS) {
                    if (!(k in prefs)) prefs[k] = DEFAULT_PREFS[k];
                }
                /* Migrate old structure_alert pref to new subcategories */
                if ('structure_alert' in prefs && !('structure_attack' in prefs)) {
                    var val = prefs['structure_alert'];
                    prefs.structure_attack = val;
                    prefs.structure_fuel = val;
                    prefs.structure_change = val;
                    prefs.sovereignty = val;
                    prefs.moonmining = val;
                    prefs.poco = val;
                }
                return prefs;
            }
        } catch(e) {}
        return Object.assign({}, DEFAULT_PREFS);
    }

    function isTypeEnabled(type) {
        var prefs = loadPrefs();
        if (type === 'inventory_critical') return prefs['inventory_low'] !== false;
        if (type === 'contract_critical') return prefs['contract_low'] !== false;
        /* Legacy: old events with type 'structure_alert' follow structure_attack pref */
        if (type === 'structure_alert') return prefs['structure_attack'] !== false;
        return prefs[type] !== false;
    }

    function getIcon() { return document.getElementById('notif-icon'); }
    function getSlash() { return document.getElementById('notif-slash'); }
    function getBadge() { return document.getElementById('notif-badge'); }
    function getDropdown() { return document.getElementById('notif-dropdown'); }
    function getList() { return document.getElementById('notif-list'); }

    function setBellState(state) {
        var icon = getIcon();
        var slash = getSlash();
        if (!icon) return;
        if (state === 'disabled') {
            icon.setAttribute('stroke', 'var(--danger)');
            if (slash) slash.style.display = '';
        } else if (state === 'enabled') {
            icon.setAttribute('stroke', 'var(--success)');
            if (slash) slash.style.display = 'none';
        } else if (state === 'alert') {
            icon.setAttribute('stroke', 'var(--accent)');
            if (slash) slash.style.display = 'none';
        }
    }

    function saveEvents() {
        try { localStorage.setItem(EVENTS_KEY, JSON.stringify(storedEvents.slice(-50))); } catch(e) {}
    }

    function loadEvents() {
        try {
            var raw = localStorage.getItem(EVENTS_KEY);
            if (raw) storedEvents = JSON.parse(raw);
        } catch(e) { storedEvents = []; }
    }

    function formatTime(ts) {
        if (!ts) return '';
        try {
            var d = new Date(ts);
            var now = new Date();
            var diff = Math.floor((now - d) / 1000);
            if (diff < 60) return 'just now';
            if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
            if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
            return Math.floor(diff / 86400) + 'd ago';
        } catch(e) { return ''; }
    }

    function _renderEvent(ev, showMute) {
        var color = TYPE_COLORS[ev.type] || 'var(--text)';
        var typeLabel = TYPE_LABELS[ev.type] || ev.type || '';
        var enabled = isTypeEnabled(ev.type);
        var opacity = enabled ? '1' : '0.4';

        var muteBtn = '<svg onclick="event.stopPropagation();muteNotifType(\'' + ev.type + '\')" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--danger)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="cursor:pointer;flex-shrink:0;opacity:0.8;" title="Mute ' + typeLabel + ' notifications"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><line x1="3" y1="3" x2="21" y2="21" stroke="var(--danger)" stroke-width="2.5"/></svg>';

        var h = '<div style="display:flex;gap:0.5rem;padding:0.5rem 0.75rem;border-bottom:1px solid var(--border);align-items:flex-start;opacity:' + opacity + ';">';
        if (ev.icon) {
            h += '<img src="' + ev.icon + '" style="width:28px;height:28px;flex-shrink:0;border-radius:2px;" onerror="this.style.display=\'none\'">';
        }
        h += '<div style="flex:1;min-width:0;">';
        h += '<div style="font-size:11px;color:' + color + ';font-weight:600;">' + (ev.title || '') + '</div>';
        h += '<div style="font-size:10px;color:var(--text);margin-top:1px;word-break:break-word;">' + (ev.body || '') + '</div>';
        h += '<div style="display:flex;align-items:center;gap:0.5rem;margin-top:2px;">';
        h += '<span style="font-size:9px;color:var(--muted);">' + formatTime(ev.timestamp) + '</span>';
        h += '<span style="font-size:8px;color:var(--muted);border:1px solid var(--border);padding:0 3px;border-radius:2px;">' + typeLabel + '</span>';
        h += '</div></div>';
        if (showMute && enabled) { h += muteBtn; }
        h += '</div>';
        return h;
    }

    function renderList() {
        var list = getList();
        if (!list) return;

        var visible = [];
        var hiddenByType = {};  /* type -> [events] */
        for (var i = 0; i < storedEvents.length; i++) {
            var ev = storedEvents[i];
            if (isTypeEnabled(ev.type)) {
                visible.push(ev);
            } else {
                var t = ev.type;
                if (!hiddenByType[t]) hiddenByType[t] = [];
                hiddenByType[t].push(ev);
            }
        }

        if (visible.length === 0 && Object.keys(hiddenByType).length === 0) {
            list.innerHTML = '<div style="padding:0.75rem;text-align:center;font-size:10px;color:var(--muted);">No notifications</div>';
            return;
        }

        var html = '';

        /* Render visible (enabled) notifications */
        for (var i = visible.length - 1; i >= 0; i--) {
            html += _renderEvent(visible[i], true);
        }
        if (visible.length === 0) {
            html += '<div style="padding:0.75rem;text-align:center;font-size:10px;color:var(--muted);">No notifications</div>';
        }

        /* Render filtered summary */
        var hiddenTypes = Object.keys(hiddenByType);
        if (hiddenTypes.length > 0) {
            html += '<div style="padding:0.4rem 0.75rem;border-bottom:1px solid var(--border);background:var(--bg);">';
            html += '<div style="font-size:9px;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem;">Filtered</div>';
            for (var j = 0; j < hiddenTypes.length; j++) {
                var htype = hiddenTypes[j];
                var hcount = hiddenByType[htype].length;
                var hlabel = TYPE_LABELS[htype] || htype;
                var hcolor = TYPE_COLORS[htype] || 'var(--muted)';
                var isExpanded = expandedFilterType === htype;
                html += '<div onclick="event.stopPropagation();toggleFilteredType(\'' + htype + '\')" style="display:flex;align-items:center;gap:0.5rem;padding:0.2rem 0;cursor:pointer;opacity:0.6;">';
                html += '<span style="font-size:10px;color:' + hcolor + ';">' + hlabel + '</span>';
                html += '<span style="font-size:9px;color:var(--muted);background:var(--border);padding:0 5px;border-radius:8px;min-width:16px;text-align:center;">' + hcount + '</span>';
                html += '<span style="font-size:9px;color:var(--muted);margin-left:auto;">' + (isExpanded ? '▾' : '▸') + '</span>';
                html += '</div>';
                /* Expand this type's events if selected */
                if (isExpanded) {
                    for (var k = hiddenByType[htype].length - 1; k >= 0; k--) {
                        html += _renderEvent(hiddenByType[htype][k], false);
                    }
                }
            }
            html += '</div>';
        }

        list.innerHTML = html;
    }

    function updateBadge() {
        var badge = getBadge();
        if (!badge) return;
        var visibleCount = storedEvents.filter(function(ev) { return isTypeEnabled(ev.type); }).length;
        if (visibleCount > 0) {
            badge.textContent = visibleCount;
            badge.style.display = '';
            setBellState('alert');
        } else {
            badge.textContent = '0';
            badge.style.display = 'none';
            var enabled = localStorage.getItem(STORAGE_KEY) === 'true';
            setBellState(enabled ? 'enabled' : 'disabled');
        }
    }

    /* Tab lock */
    function acquireLock() {
        var now = Date.now();
        var lock = localStorage.getItem(LOCK_KEY);
        if (lock && (now - parseInt(lock, 10)) < POLL_INTERVAL + 5000) return false;
        localStorage.setItem(LOCK_KEY, now.toString());
        return true;
    }
    function releaseLock() { localStorage.removeItem(LOCK_KEY); }

    function showNotification(event) {
        if (Notification.permission !== 'granted') return;
        try {
            var n = new Notification(event.title || 'Vigilant', {
                body: event.body || '',
                icon: event.icon || '/static/logo.png',
                tag: event.type + '_' + (event.timestamp || Date.now()),
            });
            setTimeout(function() { n.close(); }, 10000);
        } catch(e) {}
    }

    async function poll() {
        if (!acquireLock()) return;
        try {
            var resp = await fetch('/notifications/poll');
            if (!resp.ok) return;
            var events = await resp.json();
            if (!Array.isArray(events) || events.length === 0) return;

            events.forEach(function(ev) {
                storedEvents.push(ev);
                if (isTypeEnabled(ev.type)) {
                    showNotification(ev);
                }
            });
            saveEvents();
            updateBadge();
            renderList();
        } catch(e) {}
    }

    function startPolling() {
        if (pollTimer) return;
        poll();
        pollTimer = setInterval(poll, POLL_INTERVAL);
    }

    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        releaseLock();
    }

    /* Settings panel toggle */
    window.toggleNotifSettings = function() {
        var panel = document.getElementById('notif-settings');
        if (!panel) return;
        var visible = panel.style.display !== 'none';
        panel.style.display = visible ? 'none' : '';
        if (!visible) {
            var prefs = loadPrefs();
            var boxes = panel.querySelectorAll('input[data-notif-type]');
            boxes.forEach(function(cb) {
                var type = cb.getAttribute('data-notif-type');
                cb.checked = prefs[type] !== false;
            });
        }
    };

    window.saveNotifPrefs = function() {
        var panel = document.getElementById('notif-settings');
        if (!panel) return;
        var prefs = loadPrefs();
        var boxes = panel.querySelectorAll('input[data-notif-type]');
        boxes.forEach(function(cb) {
            prefs[cb.getAttribute('data-notif-type')] = cb.checked;
        });
        try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch(e) {}
        updateBadge();
        renderList();
    };

    window.muteNotifType = function(type) {
        var prefs = loadPrefs();
        /* inventory_critical follows inventory_low */
        var key = type === 'inventory_critical' ? 'inventory_low' : type;
        /* Legacy structure_alert maps to structure_attack */
        if (key === 'structure_alert') key = 'structure_attack';
        prefs[key] = false;
        try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch(e) {}
        /* Also update the settings panel checkbox if visible */
        var cb = document.querySelector('input[data-notif-type="' + key + '"]');
        if (cb) cb.checked = false;
        updateBadge();
        renderList();
    };

    window.toggleFilteredType = function(type) {
        expandedFilterType = expandedFilterType === type ? null : type;
        renderList();
    };

    window.toggleShowHidden = function(btn) {
        showHidden = !showHidden;
        expandedFilterType = null;
        if (btn) {
            btn.textContent = showHidden ? 'Hide Filtered' : 'Show Filtered';
            btn.style.color = showHidden ? 'var(--accent)' : 'var(--muted)';
        }
        renderList();
    };

    /* Bell click handler */
    window.onBellClick = function() {
        var enabled = localStorage.getItem(STORAGE_KEY) === 'true';
        if (!enabled) {
            if (!('Notification' in window)) { alert('Your browser does not support notifications.'); return; }
            Notification.requestPermission().then(function(perm) {
                if (perm === 'granted') {
                    localStorage.setItem(STORAGE_KEY, 'true');
                    setBellState('enabled');
                    startPolling();
                }
            });
            return;
        }

        var dd = getDropdown();
        if (!dd) return;
        dropdownOpen = !dropdownOpen;
        dd.style.display = dropdownOpen ? '' : 'none';
        if (dropdownOpen) renderList();
    };

    window.clearNotifBadge = function() {
        storedEvents = [];
        saveEvents();
        updateBadge();
        renderList();
        fetch('/notifications/dismiss', {method: 'POST'});
    };

    window.toggleVigilantNotifications = function() {
        var enabled = localStorage.getItem(STORAGE_KEY) === 'true';
        if (enabled) {
            localStorage.setItem(STORAGE_KEY, 'false');
            stopPolling();
            storedEvents = [];
            saveEvents();
            setBellState('disabled');
            updateBadge();
        } else {
            window.onBellClick();
        }
    };

    /* Close dropdown on outside click */
    document.addEventListener('click', function(e) {
        if (dropdownOpen && !document.getElementById('notif-btn').contains(e.target)) {
            dropdownOpen = false;
            var dd = getDropdown();
            if (dd) dd.style.display = 'none';
        }
    });

    /* Init */
    document.addEventListener('DOMContentLoaded', function() {
        loadEvents();
        var enabled = localStorage.getItem(STORAGE_KEY) === 'true';
        if (enabled && Notification.permission === 'granted') {
            setBellState(storedEvents.length > 0 ? 'alert' : 'enabled');
            updateBadge();
            startPolling();
        } else {
            setBellState('disabled');
        }
    });

    window.addEventListener('beforeunload', releaseLock);
})();
