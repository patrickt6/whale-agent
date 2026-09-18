// Install box tabs, copy button, and newsletter signup.
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.install-tabs [role="tab"]'));
  var code = document.getElementById('install-cmd');
  var panel = document.getElementById('cmd-panel');
  var copyBtn = document.getElementById('copy-btn');
  var status = document.getElementById('copy-status');
  if (tabs.length && code) {
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      code.textContent = tab.getAttribute('data-cmd');
      panel.setAttribute('aria-labelledby', tab.id);
      if (focus) tab.focus();
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab, false); });
      tab.addEventListener('keydown', function (e) {
        var n = null;
        if (e.key === 'ArrowRight') n = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') n = 0;
        else if (e.key === 'End') n = tabs.length - 1;
        if (n !== null) { e.preventDefault(); select(tabs[n], true); }
      });
    });
    copyBtn.addEventListener('click', function () {
      var text = code.textContent;
      var label = copyBtn.querySelector('.copy-label');
      function done(ok) {
        label.textContent = ok ? 'Copied' : 'Press Ctrl+C';
        status.textContent = ok ? 'Command copied' : 'Copy failed';
        copyBtn.classList.toggle('copied', ok);
        setTimeout(function () { label.textContent = 'Copy'; copyBtn.classList.remove('copied'); }, 1800);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      } else {
        var r = document.createRange(); r.selectNodeContents(code);
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
        var ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
        done(ok);
      }
    });
  }

  var form = document.getElementById('signup');
  if (form) {
    var msg = document.getElementById('signup-msg');
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = form.email.value.trim();
      msg.className = 'signup-msg';
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        msg.textContent = 'Please enter a valid email address.';
        msg.classList.add('err');
        return;
      }
      var btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      msg.textContent = 'Sending...';
      fetch('/api/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, website: form.website.value })
      }).then(function (r) {
        return r.json().catch(function () { return { ok: false, message: 'Something went wrong. Try again later.' }; });
      }).then(function (d) {
        msg.textContent = d.message || (d.ok ? 'You are on the list.' : 'Something went wrong.');
        msg.classList.add(d.ok ? 'ok' : 'err');
        if (d.ok) form.email.value = '';
      }).catch(function () {
        msg.textContent = 'Network error. Try again later.';
        msg.classList.add('err');
      }).then(function () { btn.disabled = false; });
    });
  }
})();
