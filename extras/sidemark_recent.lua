--
-- Sidemark recent files as a walker/elephant menu (Omarchy launcher).
--
-- Install: copy to ~/.config/elephant/menus/ (install.sh does this
-- automatically when that directory exists), then restart elephant:
--   systemctl --user restart elephant
-- Open via walker's provider list, or bind a prefix in walker's config.
--
Name = "sidemarkrecent"
NamePretty = "Sidemark Recent Files"
Icon = "sidemark"
Cache = false
SearchName = true
FixedOrder = true

function GetEntries()
  local entries = {}
  local data = os.getenv("XDG_DATA_HOME") or (os.getenv("HOME") .. "/.local/share")
  local f = io.popen("jq -r '.[].path' " .. data .. "/sidemark/recent.json 2>/dev/null")
  if not f then return entries end
  for path in f:lines() do
    local name = path:match("([^/]+)$") or path
    table.insert(entries, {
      Text = name,
      Subtext = path,
      Value = path,
      Actions = { open = "sidemark \"%VALUE%\"" },
    })
  end
  f:close()
  return entries
end
