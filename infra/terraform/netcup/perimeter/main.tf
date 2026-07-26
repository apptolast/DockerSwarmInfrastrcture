locals {
  firewall_policy_name = "apptolast-${var.environment}-ingress"
  public_tcp_rules = flatten([
    for port in sort(tolist(local.public_tcp_ports)) : [
      {
        port   = port
        source = "0.0.0.0/0"
      },
      {
        port   = port
        source = "::/0"
      }
    ]
  ])
}

resource "netcup_firewall_policy" "host_ingress" {
  count = var.manage_firewall ? 1 : 0

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

  server_id = coalesce(var.server_id, 0)
  mac       = coalesce(var.server_mac, "")
  policy_ids = concat(
    local.preserved_policy_ids,
    [netcup_firewall_policy.host_ingress[0].id]
  )
  active = true

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = var.server_id != null && var.server_mac != null
      error_message = "server_id and server_mac are required before assignment."
    }

    precondition {
      condition     = var.preserved_policy_ids != null
      error_message = <<-EOT
        preserved_policy_ids must explicitly list every currently assigned
        Netcup policy. Use [] only after confirming that none exist.
      EOT
    }
  }
}

resource "netcup_ssh_key" "image_installation" {
  for_each = var.scp_ssh_public_keys

  name = each.value.name
  key  = trimspace(each.value.key)

  lifecycle {
    prevent_destroy = true
  }
}
