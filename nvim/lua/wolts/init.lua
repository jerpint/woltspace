-- wolts.nvim  by Ali Harakeh (asharakeh)
--
-- Talk to woltspace wolts from Neovim, like a Claude / Codex panel.
--
--   <leader>Wp   pick a wolt (any lodge), then a session or "new session";
--                it opens in a terminal split on the right
--   <leader>Ww   show / hide the current wolt's split
--   <leader>Wa   ask the current wolt about this line (visual: the selection);
--                a small box opens, type the question, <C-s> or <CR> sends
--   <leader>Wz   zoom: the wolt alone, full screen; again to go back
--                (<M-z> does the same, also while typing in the wolt)
--   <leader>Wo   switch between the wolts open in this Neovim, last used first
--                (<M-w> does the same, also while typing in the wolt)
--   <M-h/j/k/l>  move between windows, from your code into the wolt and back
--   <M-=> <M-->  wider / narrower wolt split, from anywhere (<M-+> is wider too)
--                (window_keys = false leaves these keys to you)
--   :Wolts pick | toggle | ask | zoom | switch
--
-- Each lodge is reached over its HTTP API with curl. A session is shown with
-- `tmux attach`, the same way `woltspace tui` does it: directly for a lodge on
-- this machine, through `docker exec` for a lodge in a container. Questions go
-- through the lodge's message API, marked as coming from you.
--
-- Needs: Neovim 0.11+, curl, tmux (and docker for a lodge in a container).
--
--   require('wolts').setup {
--     lodges = {
--       { name = 'home', url = 'http://127.0.0.1:7777' },
--       -- a lodge in a container, with a neutral sender and repo-relative paths:
--       -- { name = 'box', url = 'http://127.0.0.1:7778', container = true, from = 'human', relative_paths = true },
--     },
--   }

local M = {}

-- The existing TUI key contract, for the local lodge only. Other lodges opt in
-- to their own key/callback; a local credential must not follow a changed URL.
function M.local_key()
  local explicit = vim.trim(vim.env.WOLTSPACE_LODGE_KEY or '')
  if explicit ~= '' then return explicit end
  local root = vim.env.WOLTSPACE_WOLTS_DIR or vim.env.WOLTS_DIR
  if not root then return '' end
  local scope = ((vim.env.WOLTSPACE_WOLT_SESSION or '') ~= '' or (vim.env.WOLT_SESSION or '') ~= '') and 'wolt' or 'owner'
  local ok, lines = pcall(vim.fn.readfile, root .. '/.space/run/lodge/' .. scope .. '.key')
  return ok and vim.trim(table.concat(lines, '\n')) or ''
end

local defaults = {
  -- Each lodge:
  --   name            short label shown in the picker
  --   url             the lodge's HTTP API
  --   container       attach through `docker exec`: true finds the container like
  --                   `woltspace tui` does (first running one named *woltspace*),
  --                   or give its name
  --   container_user  user inside the container (default 'node')
  --   from            sender name on messages (default: `user` below)
  --   relative_paths  prefer git-root-relative refs; else shorten home to ~/.
  --                   Files outside both can still have absolute paths.
  --   key             this lodge's key string or callback (no implicit key
  --                   for entries in a custom lodges list)
  lodges = { { name = 'home', url = vim.env.WOLTSPACE_API or vim.env.WOLTSPACE_URL or 'http://127.0.0.1:7777', key = M.local_key } },
  -- Who messages say they are from (same rule as `woltspace tui`)
  user = vim.env.WOLTSPACE_USER or vim.env.HUMAN_NAME or vim.env.USER or 'human',
  resting_shown = 8, -- resting sessions offered per wolt, newest first
  width = 0.45, -- the wolt split, as a share of the screen
  keys = true, -- false: no keys, use :Wolts or require('wolts') yourself
  window_keys = true, -- with keys: Alt+h/j/k/l moves between windows (unless you mapped them), Alt+= / Alt+- resize the wolt split
  resize_step = 10, -- columns per Alt+= / Alt+-
}

local config = vim.deepcopy(defaults)

local state = {
  current = nil, -- { lodge = <lodge>, wolt = <name>, session = <slug> }
  bufs = {}, -- [lodge.name .. '/' .. slug] = terminal buffer
  meta = {}, -- same keys: { key, lodge, wolt, emoji, session, title, used }
}

local function notify(msg, level) vim.notify(msg, level or vim.log.levels.INFO, { title = 'wolts' }) end

-- HTTP --------------------------------------------------------------------

---Call the lodge API. cb(err, decoded_json) runs on the main loop.
local function api(lodge, method, path, body, cb, timeout)
  local ok_key, key_value = pcall(function()
    if type(lodge.key) == 'function' then return lodge.key() end
    return lodge.key
  end)
  if not ok_key or (key_value ~= nil and type(key_value) ~= 'string') then return cb(lodge.name .. ': key unavailable') end
  key_value = vim.trim(key_value or '')
  if key_value:find '[\r\n]' then return cb(lodge.name .. ': invalid key') end
  local function quote(value)
    return '"' .. value:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r') .. '"'
  end
  -- Disable curlrc and never follow redirects. Keys and JSON travel on stdin,
  -- not in process arguments; error bodies/stderr may echo private data.
  local cmd = { 'curl', '-q', '-sS', '--max-time', tostring(timeout or 10), '-X', method, '-w', '\n%{http_code}', '--config', '-' }
  local input = ''
  if key_value ~= '' then input = 'header = ' .. quote('X-Woltspace-Key: ' .. key_value) .. '\n' end
  if body then
    vim.list_extend(cmd, { '-H', 'Content-Type: application/json' })
    input = input .. 'data-binary = ' .. quote(vim.json.encode(body)) .. '\n'
  end
  table.insert(cmd, lodge.url:gsub('/+$', '') .. path)
  vim.system(cmd, { text = true, stdin = input }, function(r)
    vim.schedule(function()
      if r.code ~= 0 then return cb(lodge.name .. ': request failed (curl ' .. r.code .. ')') end
      local out, status = r.stdout:match '^(.*)\n(%d+)$'
      local code = tonumber(status) or 0
      if code < 200 or code >= 300 then return cb(('%s: HTTP %s'):format(lodge.name, status or 'unknown')) end
      local ok, data = pcall(vim.json.decode, out or '', { luanil = { object = true, array = true } })
      if not ok then return cb(lodge.name .. ': bad JSON from ' .. path) end
      cb(nil, data)
    end)
  end)
end

local function alive(s)
  if type(s.agent_alive) == 'boolean' then return s.agent_alive end
  return s.alive == true
end

-- Fetch wolts + sessions from every lodge, then cb(entries, errors).
local function fetch_all(cb)
  local lodges = config.lodges
  local entries, errors, pending = {}, {}, #lodges * 2
  local per = {}
  local function done()
    pending = pending - 1
    if pending > 0 then return end
    for idx, lodge in ipairs(lodges) do
      local p = per[lodge.name]
      if p and p.wolts and p.sessions then
        local by_wolt = {}
        for _, s in ipairs(p.sessions) do
          by_wolt[s.wolt] = by_wolt[s.wolt] or {}
          table.insert(by_wolt[s.wolt], s)
        end
        for _, w in ipairs(p.wolts) do
          local ss = by_wolt[w.name] or {}
          table.sort(ss, function(a, b) return (a.created_at or 0) > (b.created_at or 0) end)
          local live = 0
          for _, s in ipairs(ss) do
            if alive(s) then live = live + 1 end
          end
          table.insert(entries, { lodge = lodge, idx = idx, wolt = w, sessions = ss, live = live })
        end
      end
    end
    cb(entries, errors)
  end
  for _, lodge in ipairs(lodges) do
    per[lodge.name] = {}
    for _, what in ipairs { 'wolts', 'sessions' } do
      api(lodge, 'GET', '/' .. what, nil, function(err, data)
        if err then
          errors[lodge.name] = err
        else
          per[lodge.name][what] = data
        end
        done()
      end, 5)
    end
  end
end

-- Terminal split ------------------------------------------------------------

local function key(lodge, slug) return lodge.name .. '/' .. slug end

local container_cache = {}
local function container_for(lodge)
  if type(lodge.container) == 'string' then return lodge.container end
  if container_cache[lodge.name] then return container_cache[lodge.name] end
  local r = vim.system({ 'docker', 'ps', '--format', '{{.Names}}', '--filter', 'name=woltspace' }, { text = true }):wait(5000)
  local name = r.code == 0 and vim.split(vim.trim(r.stdout or ''), '\n')[1] or ''
  if name == '' then name = 'woltspace' end
  container_cache[lodge.name] = name
  return name
end

local function attach_cmd(lodge, slug)
  if lodge.container then
    local u = lodge.container_user or 'node'
    return { 'docker', 'exec', '-it', '-u', u, '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8', container_for(lodge), 'tmux', '-u', 'attach', '-t', slug }
  end
  -- env -u TMUX: if Neovim itself runs inside tmux, attach as a nested client
  return { 'env', '-u', 'TMUX', 'tmux', '-u', 'attach', '-t', slug }
end

local function wolt_win()
  local cur = state.current
  local buf = cur and state.bufs[key(cur.lodge, cur.session)]
  if not buf then return nil end
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.api.nvim_win_get_buf(win) == buf then return win end
  end
end

-- Any window already showing a wolt buffer (reused when switching wolts).
local function any_wolt_win()
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.b[vim.api.nvim_win_get_buf(win)].wolt_session then return win end
  end
end

local function terminal_keys(buf)
  local o = { buffer = buf }
  -- Claude and Codex use Esc themselves: if your config maps <Esc><Esc> to
  -- leave terminal mode, keep it for them here
  vim.keymap.set('t', '<Esc><Esc>', '<Esc><Esc>', o)
  -- Alt+hjkl leaves the wolt and moves to the next window
  for _, d in ipairs { 'h', 'j', 'k', 'l' } do
    vim.keymap.set('t', '<M-' .. d .. '>', '<C-\\><C-n><C-w>' .. d, o)
  end
  -- Alt+z zooms in / back out while you're typing in the wolt
  vim.keymap.set('t', '<M-z>', function() M.zoom() end, o)
  -- Alt+= / Alt+- resize the wolt split while you're typing in it
  if config.keys and config.window_keys then
    for lhs, sign in pairs { ['<M-=>'] = 1, ['<M-+>'] = 1, ['<M-->'] = -1 } do
      vim.keymap.set('t', lhs, function() M.resize(sign * config.resize_step) end, o)
    end
  end
  -- Alt+w: leave terminal mode, then the switch picker (next tick, so it gets the keys)
  if config.keys then
    vim.keymap.set('t', '<M-w>', function()
      vim.schedule(M.switch)
      return '<C-\\><C-n>'
    end, { buffer = buf, expr = true, desc = 'Wolts: switch between open wolts' })
  end
  vim.api.nvim_create_autocmd('BufEnter', { buffer = buf, command = 'startinsert' })
end

-- Make session k the current one and the last used (when the switch picker knows it).
local function make_current(k)
  local m = state.meta[k]
  if not m then return end
  m.used = vim.uv.hrtime()
  state.current = { lodge = m.lodge, wolt = m.wolt, session = m.session }
end

---Show the current wolt in the right split, creating its terminal if needed.
---@param focus boolean move the cursor into it
local function show(focus)
  local cur = state.current
  if not cur then return end
  local k = key(cur.lodge, cur.session)
  local buf = state.bufs[k]
  local back = vim.api.nvim_get_current_win()

  local win = wolt_win() or any_wolt_win()
  if win then
    vim.api.nvim_set_current_win(win)
  else
    vim.cmd 'botright vsplit'
    vim.api.nvim_win_set_width(0, math.floor(vim.o.columns * config.width))
  end
  win = vim.api.nvim_get_current_win()
  vim.wo[win].number = false
  vim.wo[win].relativenumber = false
  vim.wo[win].signcolumn = 'no'
  vim.wo[win].winfixwidth = true

  if buf and vim.api.nvim_buf_is_valid(buf) then
    vim.api.nvim_win_set_buf(win, buf)
  else
    buf = vim.api.nvim_create_buf(false, false)
    vim.api.nvim_win_set_buf(win, buf)
    state.bufs[k] = buf
    vim.b[buf].wolt_session = k
    local cmd = attach_cmd(cur.lodge, cur.session)
    vim.fn.jobstart(cmd, {
      term = true,
      on_exit = function(_, code)
        -- Session ended (agent quit) or the viewer was closed: forget the buffer
        state.bufs[k] = nil
        state.meta[k] = nil
        vim.schedule(function()
          if code ~= 0 then notify(('%s: attach exited %d (%s)'):format(k, code, table.concat(cmd, ' ')), vim.log.levels.WARN) end
          if vim.api.nvim_buf_is_valid(buf) then vim.api.nvim_buf_delete(buf, { force = true }) end
        end)
      end,
    })
    pcall(vim.api.nvim_buf_set_name, buf, 'wolt://' .. k)
    terminal_keys(buf)
    -- :b, a buffer picker or <C-o> to a wolt: <leader>Wa then asks that wolt
    vim.api.nvim_create_autocmd('BufEnter', { buffer = buf, callback = function() make_current(k) end })
  end
  -- entering the split may have fired BufEnter for the wolt it showed before
  state.current = cur
  make_current(k)

  if focus then
    vim.cmd.startinsert()
  elseif vim.api.nvim_win_is_valid(back) then
    vim.api.nvim_set_current_win(back)
  end
end

---Zoom: the current wolt alone in its own tab, full screen. Again: close that
---tab and go back to your layout, with the wolt split where it was.
function M.zoom()
  if vim.t.wolt_zoom then
    if vim.fn.tabpagenr '$' == 1 then return notify 'this is the only tab: nothing to go back to' end
    local from = vim.t.wolt_zoom_from
    vim.cmd.tabclose()
    if from and vim.api.nvim_tabpage_is_valid(from) then vim.api.nvim_set_current_tabpage(from) end
    show(true)
    -- back to typing in the wolt (after a key from terminal mode has finished)
    vim.schedule(vim.cmd.startinsert)
    return
  end
  local cur = state.current
  local buf = cur and state.bufs[key(cur.lodge, cur.session)]
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then return notify('no wolt open yet: :Wolts pick first', vim.log.levels.WARN) end
  -- hide its split first: a terminal shown in two windows takes the smaller size
  local win = wolt_win()
  if win then pcall(vim.api.nvim_win_hide, win) end
  local from = vim.api.nvim_get_current_tabpage()
  vim.cmd('tab sbuffer ' .. buf)
  vim.t.wolt_zoom = true
  vim.t.wolt_zoom_from = from
  local w = vim.api.nvim_get_current_win()
  vim.wo[w].number = false
  vim.wo[w].relativenumber = false
  vim.wo[w].signcolumn = 'no'
  vim.cmd.startinsert()
end

function M.toggle()
  local win = wolt_win()
  if win then
    vim.api.nvim_win_hide(win)
  elseif state.current then
    show(true)
  else
    M.pick()
  end
end

-- Make sure the session has a live agent (wake it if resting), then cb().
local function ensure_alive(lodge, s, cb)
  if alive(s) then return cb() end
  notify(('waking %s on %s ...'):format(s.name, lodge.name))
  api(lodge, 'POST', '/sessions/' .. s.name .. '/resume', {}, function(err)
    if err then return notify('could not wake it: ' .. err, vim.log.levels.ERROR) end
    cb()
  end, 120)
end

---@param info? { emoji?: string, title?: string } shown by the switch picker
local function use(lodge, wolt, slug, then_, info)
  info = info or {}
  local k = key(lodge, slug)
  state.meta[k] = { key = k, lodge = lodge, wolt = wolt, emoji = info.emoji, session = slug, title = info.title, used = vim.uv.hrtime() }
  state.current = { lodge = lodge, wolt = wolt, session = slug }
  if then_ then
    then_()
  else
    show(true)
  end
end

-- Picker ----------------------------------------------------------------------

local function pick_session(entry, then_)
  local items = { { new = true } }
  local resting = 0
  for _, s in ipairs(entry.sessions) do
    if alive(s) then
      table.insert(items, s)
    elseif s.status == 'resting' and resting < config.resting_shown then
      resting = resting + 1
      table.insert(items, s)
    end
  end
  -- live sessions first, then resting, each newest first
  table.sort(items, function(a, b)
    if a.new or b.new then return a.new == true and not b.new end
    if alive(a) ~= alive(b) then return alive(a) end
    return (a.created_at or 0) > (b.created_at or 0)
  end)
  local name = entry.wolt.name
  vim.ui.select(items, {
    prompt = ('%s %s on %s'):format(entry.wolt.emoji or '', name, entry.lodge.name),
    format_item = function(s)
      if s.new then return '+ new session' end
      local when = os.date('%d %b %H:%M', s.created_at or 0)
      local mark = state.bufs[key(entry.lodge, s.name)] and ' [open]' or ''
      local title = (s.title ~= nil and s.title ~= '') and ('  ' .. s.title) or ''
      return ('%s %s  %s%s%s'):format(alive(s) and '●' or '○', when, s.name, mark, title)
    end,
  }, function(s)
    if not s then return end
    if s.new then
      notify(('starting a new %s session on %s ...'):format(name, entry.lodge.name))
      api(entry.lodge, 'POST', '/sessions/new/lodge', { wolt = name }, function(err, r)
        if err then return notify('could not start it: ' .. err, vim.log.levels.ERROR) end
        if not (r and r.name) then return notify('lodge returned no session name', vim.log.levels.ERROR) end
        use(entry.lodge, name, r.name, then_, { emoji = entry.wolt.emoji })
      end, 120)
    else
      ensure_alive(entry.lodge, s, function() use(entry.lodge, name, s.name, then_, { emoji = entry.wolt.emoji, title = s.title }) end)
    end
  end)
end

---Pick a wolt from every lodge, then one of its sessions.
---@param then_? fun() run instead of showing the split (used by ask)
function M.pick(then_)
  fetch_all(function(entries, errors)
    for _, err in pairs(errors) do
      notify(err, vim.log.levels.WARN)
    end
    if #entries == 0 then return notify('no lodge answered', vim.log.levels.ERROR) end
    -- lodge order, then wolts with live sessions, then by name
    table.sort(entries, function(a, b)
      if a.idx ~= b.idx then return a.idx < b.idx end
      if (a.live > 0) ~= (b.live > 0) then return a.live > 0 end
      return a.wolt.name < b.wolt.name
    end)
    vim.ui.select(entries, {
      prompt = 'wolt',
      format_item = function(e)
        local role = e.wolt.display_name or e.wolt.role or ''
        return ('%-5s %s %-24s %s%s'):format(e.lodge.name, e.wolt.emoji or ' ', e.wolt.name, e.live > 0 and ('● ' .. e.live .. ' live  ') or '', role)
      end,
    }, function(e)
      if e then pick_session(e, then_) end
    end)
  end)
end

-- Wake the session if it went to rest since we picked it, then cb().
local function wake(cur, cb)
  api(cur.lodge, 'GET', '/sessions', nil, function(err, sessions)
    if err then return cb() end
    for _, s in ipairs(sessions) do
      if s.name == cur.session then return ensure_alive(cur.lodge, s, cb) end
    end
    cb()
  end, 5)
end

---Switch between the wolt sessions open in this Neovim, last used first.
function M.switch()
  local open = {}
  for k, m in pairs(state.meta) do
    local buf = state.bufs[k]
    if buf and vim.api.nvim_buf_is_valid(buf) then table.insert(open, m) end
  end
  if #open == 0 then return M.pick() end
  table.sort(open, function(a, b) return a.used > b.used end)
  table.insert(open, { other = true })
  local cur = state.current and key(state.current.lodge, state.current.session)
  vim.ui.select(open, {
    prompt = 'open wolts',
    format_item = function(m)
      if m.other then return '+ pick another wolt...' end
      local label = (m.title ~= nil and m.title ~= '') and m.title or m.session
      return ('%s  %s %s  %s%s'):format(m.lodge.name, m.emoji or ' ', m.wolt, label, m.key == cur and '  (current)' or '')
    end,
  }, function(m)
    if not m then return end
    if m.other then return M.pick() end
    make_current(m.key)
    -- the chosen one, even if a BufEnter moved state.current while waking it
    local chosen = state.current
    wake(chosen, function()
      state.current = chosen
      show(true)
    end)
  end)
end

-- Ask box -----------------------------------------------------------------------

local function send(text)
  local cur = state.current
  local function post()
    local body = { text = text, from_wolt = cur.lodge.from or config.user, from_session = '' }
    api(cur.lodge, 'POST', '/sessions/' .. cur.session .. '/message', body, function(err)
      if err then
        -- keep the question so it is not lost
        vim.fn.setreg('"', text)
        return notify('delivery not confirmed (question is in the " register): ' .. err, vim.log.levels.ERROR)
      end
      notify('sent to ' .. cur.wolt)
      show(false)
    end)
  end
  wake(cur, post)
end

-- "@file#L12" or "@file#L12-20" for the current wolt's lodge ('' without a file).
local function make_ref(lodge, file, l1, l2)
  if not file then return '' end
  if lodge.relative_paths then
    local root = vim.fs.root(file, '.git')
    local rel = root and vim.fs.relpath(root, file)
    file = rel or vim.fn.fnamemodify(file, ':~')
  else
    file = vim.fn.fnamemodify(file, ':~')
  end
  if l1 == l2 then return ('@%s#L%d'):format(file, l1) end
  return ('@%s#L%d-%d'):format(file, l1, l2)
end

local function open_box(file, l1, l2)
  local cur = state.current
  local ref = make_ref(cur.lodge, file, l1, l2)
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].filetype = 'markdown'
  vim.bo[buf].bufhidden = 'wipe'
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, { ref, '', '' })
  local width = math.min(90, math.floor(vim.o.columns * 0.7))
  local height = 10
  local win = vim.api.nvim_open_win(buf, true, {
    relative = 'editor',
    width = width,
    height = height,
    row = math.floor((vim.o.lines - height) / 2),
    col = math.floor((vim.o.columns - width) / 2),
    style = 'minimal',
    border = 'rounded',
    title = (' ask %s (%s) '):format(cur.wolt, cur.lodge.name),
    title_pos = 'center',
    footer = ' <C-s> or <CR> in normal: send   q / <Esc>: cancel ',
    footer_pos = 'center',
  })
  vim.wo[win].wrap = true
  local function close()
    if vim.api.nvim_win_is_valid(win) then vim.api.nvim_win_close(win, true) end
  end
  local function submit()
    local text = vim.trim(table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), '\n'))
    vim.cmd.stopinsert()
    close()
    if text == '' or text == ref then return notify 'nothing to send' end
    send(text)
  end
  local o = { buffer = buf, nowait = true }
  vim.keymap.set({ 'n', 'i' }, '<C-s>', submit, o)
  vim.keymap.set('n', '<CR>', submit, o)
  vim.keymap.set('n', 'q', close, o)
  vim.keymap.set('n', '<Esc>', close, o)
  vim.api.nvim_win_set_cursor(win, { 3, 0 })
  vim.cmd.startinsert()
end

---Ask the current wolt about lines l1..l2 of the current file.
function M.ask(l1, l2)
  local file = vim.api.nvim_buf_get_name(0)
  if file == '' or vim.bo.buftype ~= '' then file = nil end -- no file here: just a question
  if state.current then
    open_box(file, l1, l2)
  else
    M.pick(function() open_box(file, l1, l2) end)
  end
end

---Make the wolt split wider (delta > 0) or narrower, by columns.
---@param delta integer
function M.resize(delta)
  local win = wolt_win() or any_wolt_win()
  if not win then return notify 'no wolt split open' end
  vim.api.nvim_win_set_width(win, math.max(20, math.min(vim.o.columns - 20, vim.api.nvim_win_get_width(win) + delta)))
end

-- Setup: keys, :Wolts ------------------------------------------------------------

local function set_keys()
  local map = vim.keymap.set
  map('n', '<leader>Wp', function() M.pick() end, { desc = 'Wolts: [P]ick a wolt / session' })
  map('n', '<leader>Ww', M.toggle, { desc = 'Wolts: show / hide the [W]olt split' })
  map('n', '<leader>Wz', function() M.zoom() end, { desc = 'Wolts: [Z]oom the wolt full screen / back' })
  map('n', '<M-z>', function() M.zoom() end, { desc = 'Wolts: zoom the wolt full screen / back' })
  map('n', '<leader>Wo', function() M.switch() end, { desc = 'Wolts: switch between [O]pen wolts' })
  map('n', '<M-w>', function() M.switch() end, { desc = 'Wolts: switch between open wolts' })
  map('n', '<leader>Wa', function()
    local l = vim.fn.line '.'
    M.ask(l, l)
  end, { desc = 'Wolts: [A]sk about this line' })
  map('x', '<leader>Wa', function()
    local a, b = vim.fn.line 'v', vim.fn.line '.'
    vim.api.nvim_feedkeys(vim.keycode '<Esc>', 'nx', false)
    M.ask(math.min(a, b), math.max(a, b))
  end, { desc = 'Wolts: [A]sk about the selection' })
  if config.window_keys then
    -- from code back into the wolt (and on to any window); the wolt's own terminal maps the same keys.
    -- Plain window moves: a map of your own for these keys is kept.
    for d, where in pairs { h = 'left', j = 'below', k = 'above', l = 'right' } do
      local lhs = '<M-' .. d .. '>'
      if vim.fn.maparg(lhs, 'n') == '' then map('n', lhs, '<C-w>' .. d, { desc = 'Move to the window ' .. where }) end
    end
    for lhs, sign in pairs { ['<M-=>'] = 1, ['<M-+>'] = 1, ['<M-->'] = -1 } do
      map('n', lhs, function() M.resize(sign * config.resize_step) end, { desc = sign > 0 and 'Wolts: wider wolt split' or 'Wolts: narrower wolt split' })
    end
  end
  local ok, wk = pcall(require, 'which-key')
  if ok then wk.add { { '<leader>W', group = '[W]olts', mode = { 'n', 'v' } } } end
end

---@param opts? table see `defaults` above
function M.setup(opts)
  if vim.fn.has 'nvim-0.11' == 0 then error('wolts.nvim requires Neovim 0.11 or newer') end
  config = vim.tbl_deep_extend('force', vim.deepcopy(defaults), opts or {})
  -- a list replaces the default list instead of merging into it
  if opts and opts.lodges then config.lodges = opts.lodges end

  vim.api.nvim_create_user_command('Wolts', function(o)
    local sub = o.fargs[1] or 'pick'
    if sub == 'pick' then
      M.pick()
    elseif sub == 'toggle' then
      M.toggle()
    elseif sub == 'ask' then
      M.ask(o.line1, o.line2)
    elseif sub == 'zoom' then
      M.zoom()
    elseif sub == 'switch' then
      M.switch()
    else
      notify('unknown: ' .. sub .. ' (pick, toggle, ask, zoom, switch)', vim.log.levels.ERROR)
    end
  end, {
    nargs = '?',
    range = true,
    complete = function() return { 'pick', 'toggle', 'ask', 'zoom', 'switch' } end,
    desc = 'Wolts: pick, toggle, ask, zoom, switch',
  })

  -- A saved layout (persistence.nvim) can't restore a terminal: hide wolt splits before it is saved
  vim.api.nvim_create_autocmd('User', {
    pattern = 'PersistenceSavePre',
    desc = 'Hide wolt splits before the layout is saved',
    group = vim.api.nvim_create_augroup('wolts', { clear = true }),
    callback = function()
      for _, win in ipairs(vim.api.nvim_list_wins()) do
        if vim.b[vim.api.nvim_win_get_buf(win)].wolt_session then pcall(vim.api.nvim_win_hide, win) end
      end
    end,
  })

  if config.keys then set_keys() end
end

return M
