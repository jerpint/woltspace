-- Refuse direct runs before loading the plugin or writing any fixture files.
local sandbox = vim.env.WOLTS_TEST_ROOT
local stat = sandbox and vim.uv.fs_lstat(sandbox)
assert(stat and stat.type == 'directory' and stat.mode % 512 == 448
  and vim.fs.basename(sandbox):match('^wolts%-nvim%-')
  and vim.uv.fs_realpath(vim.env.HOME) == sandbox .. '/home'
  and vim.uv.fs_realpath(vim.env.WOLTSPACE_WOLTS_DIR) == sandbox .. '/wolts',
  'private test root required; run pytest nvim/test_plugin.py')
assert(not vim.env.WOLTSPACE_LODGE_KEY and not vim.env.WOLTSPACE_WOLT_SESSION and not vim.env.WOLT_SESSION)
local url = assert(vim.env.WOLTS_TEST_URL, 'run pytest nvim/test_plugin.py')
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')
vim.opt.runtimepath:append(root)
local M, notices, commands = require('wolts'), {}, {}
local keydir = vim.env.WOLTSPACE_WOLTS_DIR .. '/.space/run/lodge'
vim.fn.mkdir(keydir, 'p')
vim.fn.writefile({ 'owner-dummy' }, keydir .. '/owner.key')
vim.fn.writefile({ 'wolt-dummy' }, keydir .. '/wolt.key')
vim.env.WOLTSPACE_WOLT_SESSION, vim.env.WOLT_SESSION = '', ''
assert(M.local_key() == 'owner-dummy')
vim.env.WOLT_SESSION = 'test'; assert(M.local_key() == 'wolt-dummy')
vim.env.WOLTSPACE_LODGE_KEY = 'override'; assert(M.local_key() == 'override')
vim.env.WOLTSPACE_LODGE_KEY, vim.env.WOLT_SESSION = nil, nil
vim.notify = function(msg) table.insert(notices, msg) end
local system, jobstart = vim.system, vim.fn.jobstart
local hold, held -- hold = true: keep the next curl result in held() until the test calls it
vim.system = function(cmd, opts, cb)
  assert(not table.concat(cmd, ' '):find('dummy-secret', 1, true))
  if hold then
    hold = false
    return system(cmd, opts, function(r) held = function() cb(r) end end)
  end
  return system(cmd, opts, cb)
end
vim.fn.jobstart = function(cmd, opts)
  table.insert(commands, cmd)
  return jobstart({ '/bin/cat' }, opts) -- real terminal, no real tmux or model
end
local function wait(pred) assert(vim.wait(5000, pred, 10), vim.inspect(notices)) end
local target = 'live'
vim.ui.select = function(items, opts, cb)
  if opts.prompt == 'wolt' then return cb(items[1]) end
  for _, item in ipairs(items) do if item.name == target or (target == 'fresh' and item.new) then return cb(item) end end
  error('missing picker target')
end
local function setup(path, relative, keys)
  M.setup { keys = keys == true, lodges = { { name = 'home', url = url .. path, key = function() return 'dummy-secret' end, relative_paths = relative } } }
end
local function pick(path, relative)
  setup(path, relative)
  M.pick()
  wait(function() return vim.b.wolt_session == 'home/' .. target end)
end
-- switch with nothing open: straight to the wolt picker
local pick_real, picked = M.pick, 0
M.pick = function() picked = picked + 1 end
M.switch(); assert(picked == 1)
M.pick = pick_real
-- the switch picker's lines; choose = index to pick, cmd = go through :Wolts switch
local function switch_list(choose, cmd)
  local select, shown = vim.ui.select, nil
  vim.ui.select = function(items, opts, cb)
    shown = vim.tbl_map(opts.format_item, items)
    cb(choose and items[choose] or nil)
  end
  if cmd then vim.cmd 'Wolts switch' else M.switch() end
  vim.ui.select = select
  return shown
end
local function wolt_wins()
  local wins = {}
  for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.b[vim.api.nvim_win_get_buf(w)].wolt_session then table.insert(wins, w) end
  end
  return wins
end
local function line(slug, current) return 'home    alpha  ' .. slug .. (current and '  (current)' or '') end
local home = vim.env.HOME
vim.fn.mkdir(home .. '/project/.git', 'p')
vim.fn.writefile({ 'one', 'two' }, home .. '/project/sample.lua')
vim.fn.writefile({ 'location' }, home .. '/.curlrc') -- must not override redirect refusal
pick('')
assert(commands[1][1] == 'env' and commands[1][#commands[1]] == 'live')
M.toggle(); M.toggle()
M.zoom(); assert(#vim.api.nvim_list_tabpages() == 2); M.zoom()
local function ask(expected)
  vim.cmd.edit(home .. '/project/sample.lua')
  M.ask(1, 2)
  assert(vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == expected)
  vim.api.nvim_buf_set_lines(0, 0, -1, false, { expected, '', 'quotes " and \\ and é' })
  vim.fn.maparg('<C-s>', 'n', false, true).callback()
end
ask('@~/project/sample.lua#L1-2')
wait(function() return notices[#notices] == 'sent to alpha' end)
target = 'rest'; pick(''); assert(commands[#commands][#commands[#commands]] == 'rest')
target = 'fresh'; pick(''); assert(commands[#commands][#commands[#commands]] == 'fresh')
-- open sessions, last used first, current marked, then the wolt picker
local list = switch_list(nil, true)
assert(vim.deep_equal(list, { line('fresh', true), line('rest'), line('live'), '+ pick another wolt...' }), vim.inspect(list))
-- switch to the first one opened: it is current and the one wolt split shows it
switch_list(3)
wait(function() return vim.b.wolt_session == 'home/live' end)
assert(#wolt_wins() == 1 and vim.api.nvim_win_get_buf(wolt_wins()[1]) == vim.fn.bufnr 'wolt://home/live')
assert(vim.deep_equal(switch_list(), { line('live', true), line('fresh'), line('rest'), '+ pick another wolt...' }))
-- :b to another wolt buffer makes it current
vim.cmd.buffer(vim.fn.bufnr 'wolt://home/rest')
assert(switch_list()[1] == line('rest', true))
-- a resting session is woken before it is shown
switch_list(2)
wait(function() return vim.b.wolt_session == 'home/live' end)
local woken = #notices
switch_list(2)
wait(function() return #notices > woken and notices[woken + 1] == 'waking rest on home ...' and vim.b.wolt_session == 'home/rest' end)
-- moving to other wolt buffers while the wake is pending still shows the one chosen
hold = true; switch_list(2)
wait(function() return held ~= nil end)
vim.cmd.buffer(vim.fn.bufnr 'wolt://home/live'); vim.cmd.buffer(vim.fn.bufnr 'wolt://home/rest')
held(); held = nil
wait(function() return vim.b.wolt_session == 'home/live' end)
assert(switch_list()[1] == line('live', true))
-- a session that exits leaves the list
local rest_buf = vim.fn.bufnr 'wolt://home/rest'
vim.fn.jobstop(vim.b[rest_buf].terminal_job_id)
wait(function() return not vim.api.nvim_buf_is_valid(rest_buf) end)
assert(not vim.tbl_contains(switch_list(), line('rest')) and not vim.tbl_contains(switch_list(), line('rest', true)))
-- Alt+w: normal mode everywhere, terminal mode only in wolt buffers
target = 'rest'; setup('', false, true); M.pick()
wait(function() return vim.b.wolt_session == 'home/rest' end)
assert(vim.fn.maparg('<M-w>', 'n') ~= '' and vim.fn.maparg('<leader>Wo', 'n') ~= '')
assert(vim.fn.maparg('<M-w>', 't', false, true).buffer == 1)
assert(vim.api.nvim_buf_call(vim.api.nvim_create_buf(false, true), function() return vim.fn.maparg('<M-w>', 't') end) == '')
-- keys = false: a wolt buffer gets no Alt+w either
assert(vim.api.nvim_buf_call(vim.fn.bufnr 'wolt://home/live', function() return vim.fn.maparg('<M-w>', 't') end) == '')
target = 'live'; pick('/drop', true); ask('@sample.lua#L1-2')
wait(function() return notices[#notices]:find('delivery not confirmed', 1, true) end)
assert(vim.fn.getreg('"'):find('quotes', 1, true))
for _, path in ipairs { '/deny', '/redirect' } do
  local before = #notices
  setup(path); M.pick()
  wait(function() return #notices > before and notices[#notices] == 'no lodge answered' end)
end
M.setup { keys = false, lodges = { { name = 'public', url = url .. '/public' } } }; M.pick()
wait(function() return commands[#commands][#commands[#commands]] == 'live' and vim.bo.buftype == 'terminal' end)
assert(not table.concat(notices, '\n'):find('dummy-secret', 1, true))
vim.fn.delete(home .. '/.curlrc')
vim.cmd 'qa!'
