locals {
  firewall_policy_name = "apptolast-${var.environment}-ingress"
  public_tcp_rules = flatten([
    for port in sort(tolist(local.effective_public_tcp_ports)) : [
      {
        port   = port
        source = "0.0.0.0/0"
      }
    ]
  ])
}

resource "netcup_firewall_policy" "host_ingress" {
  count = var.manage_firewall ? 1 : 0

  depends_on = [terraform_data.root_identity]

  name        = local.firewall_policy_name
  description = "Managed by Terraform: restricted SSH and explicit web ports"

  dynamic "rule" {
    for_each = sort(tolist(var.admin_cidrs))
    content {
      action            = "ACCEPT"
      protocol          = "TCP"
      direction         = "INGRESS"
      destination_ports = tostring(var.ssh_port)
      sources           = [rule.value]
      description       = "Administrative SSH from approved CIDR"
    }
  }

  dynamic "rule" {
    for_each = local.public_tcp_rules
    content {
      action            = "ACCEPT"
      protocol          = "TCP"
      direction         = "INGRESS"
      destination_ports = tostring(rule.value.port)
      sources           = [rule.value.source]
      description       = "Explicitly public TCP service"
    }
  }

  rule {
    action      = "ACCEPT"
    protocol    = "ICMP"
    direction   = "INGRESS"
    sources     = ["0.0.0.0/0"]
    description = "IPv4 path MTU and diagnostics"
  }

  rule {
    action      = "ACCEPT"
    protocol    = "ICMPv6"
    direction   = "INGRESS"
    sources     = ["::/0"]
    description = "Required IPv6 control traffic and diagnostics"
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = length(var.admin_cidrs) > 0
      error_message = "At least one restricted administrator CIDR is required."
    }
  }
}

resource "netcup_server_firewall" "host" {
  count = var.manage_firewall ? 1 : 0

  depends_on = [terraform_data.root_identity]

  server_id  = local.netcup_server_id
  mac        = local.netcup_server_mac
  policy_ids = [netcup_firewall_policy.host_ingress[0].id]
  active     = true

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = (
        var.firewall_policy_inventory_confirmed &&
        length(var.preserved_policy_ids) == 0
      )
      error_message = <<-EOT
        Inventory every assigned Netcup policy first. The reviewed assignment
        replaces it with the single fully modeled policy and never retains
        an opaque policy ID.
      EOT
    }
  }
}

resource "netcup_ssh_key" "image_installation" {
  for_each = var.scp_ssh_public_keys

  depends_on = [terraform_data.root_identity]

  name = each.value.name
  key  = trimspace(each.value.key)

  lifecycle {
    prevent_destroy = true
  }
}
