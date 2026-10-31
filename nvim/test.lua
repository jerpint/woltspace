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
local url = assert(vim.env.WOLTS_TEST_URL, 'run test/test_nvim_plugin.py')
local system, jobstart = vim.system, vim.fn.jobstart
vim.system = function(cmd, opts, cb)
  assert(not table.concat(cmd, ' '):find('dummy-secret', 1, true))
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
local function setup(path, relative)
  M.setup { keys = false, lodges = { { name = 'home', url = url .. path, key = function() return 'dummy-secret' end, relative_paths = relative } } }
end
local function pick(path, relative)
  setup(path, relative)
  M.pick()
  wait(function() return vim.b.wolt_session == 'home/' .. target end)
end
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
