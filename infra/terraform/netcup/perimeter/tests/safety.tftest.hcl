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
    admin_cidrs     = ["192.0.2.1/32"]
  }

  expect_failures = [
    netcup_server_firewall.host[0],
  ]
}

run "reviewed_identity_has_no_public_application_ipv6" {
  command = plan

  variables {
    manage_firewall                     = true
    admin_cidrs                         = ["192.0.2.1/32"]
    preserved_policy_ids                = []
    firewall_policy_inventory_confirmed = true
  }

  assert {
    condition = (
      netcup_server_firewall.host[0].active == true &&
      netcup_server_firewall.host[0].server_id == 900782 &&
      netcup_server_firewall.host[0].mac == "16:2e:0f:f4:c4:da" &&
      terraform_data.root_identity.input.root == "netcup/perimeter" &&
      alltrue([
        for rule in netcup_firewall_policy.host_ingress[0].rule :
        !contains(rule.sources, "::/0") || rule.protocol == "ICMPv6"
      ])
    )
    error_message = "The assignment identity or IPv6 policy is not reviewed."
  }
}

run "opaque_preserved_policy_ids_are_rejected" {
  command = plan

  variables {
    preserved_policy_ids = [12345]
  }

  expect_failures = [
    var.preserved_policy_ids,
  ]
}
