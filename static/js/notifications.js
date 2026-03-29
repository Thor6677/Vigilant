/* Vigilant — Browser notification handler */

(function() {
    var POLL_INTERVAL = 30000;
    var STORAGE_KEY = 'vigilant_notif_enabled';
    var EVENTS_KEY = 'vigilant_notif_events';
    var LOCK_KEY = 'vigilant_notif_lock';
    var pollTimer = null;
    var storedEvents = [];
    var dropdownOpen = false;

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

    var TYPE_COLORS = {
        skill_complete: 'var(--success)',
        job_ready: 'var(--accent)',
        pi_expiring: 'var(--danger)',
        new_mail: 'var(--text)',
    };

    function renderList() {
        var list = getList();
        if (!list) return;
        if (storedEvents.length === 0) {
            list.innerHTML = '<div style="padding:0.75rem;text-align:center;font-size:10px;color:var(--muted);">No notifications</div>';
            return;
        }
        var html = '';
        for (var i = storedEvents.length - 1; i >= 0; i--) {
            var ev = storedEvents[i];
            var color = TYPE_COLORS[ev.type] || 'var(--text)';
            html += '<div style="display:flex;gap:0.5rem;padding:0.5rem 0.75rem;border-bottom:1px solid var(--border);align-items:flex-start;" onclick="event.stopPropagation()">';
            if (ev.icon) {
                html += '<img src="' + ev.icon + '" style="width:28px;height:28px;flex-shrink:0;border-radius:2px;" onerror="this.style.display=\'none\'">';
            }
            html += '<div style="flex:1;min-width:0;">';
            html += '<div style="font-size:11px;color:' + color + ';font-weight:600;">' + (ev.title || '') + '</div>';
            html += '<div style="font-size:10px;color:var(--text);margin-top:1px;word-break:break-word;">' + (ev.body || '') + '</div>';
            html += '<div style="font-size:9px;color:var(--muted);margin-top:2px;">' + formatTime(ev.timestamp) + '</div>';
            html += '</div></div>';
        }
        list.innerHTML = html;
    }

    function updateBadge() {
        var badge = getBadge();
        if (!badge) return;
        if (storedEvents.length > 0) {
            badge.textContent = storedEvents.length;
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
                showNotification(ev);
                storedEvents.push(ev);
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

    /* Bell click handler */
    window.onBellClick = function() {
        var enabled = localStorage.getItem(STORAGE_KEY) === 'true';
        if (!enabled) {
            /* Enable notifications */
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

        /* Toggle dropdown */
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
