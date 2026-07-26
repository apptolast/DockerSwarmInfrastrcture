mock_provider "netcup" {}

run "mutations_are_disabled_by_default" {
  command = plan

  assert {
    condition = (
      length(netcup_firewall_policy.host_ingress) == 0 &&
      length(netcup_server_firewall.host) == 0 &&
      length(netcup_ssh_key.image_installation) == 0
    )
    error_message = "Netcup resources must be disabled by default."
  }
}

run "assignment_requires_existing_policy_inventory" {
  command = plan

  variables {
    manage_firewall = true
    server_id       = 123456
    server_mac      = "02:00:00:00:00:01"
    admin_cidrs     = ["192.0.2.1/32"]
  }

  expect_failures = [
    netcup_server_firewall.host[0],
  ]
}
