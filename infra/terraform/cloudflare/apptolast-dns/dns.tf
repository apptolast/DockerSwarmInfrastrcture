locals {
  records = {
    edge = {
      name    = local.platform_contract.edge_traefik_hostname
      content = local.platform_contract.platform_public_ipv4
      type    = "A"
      proxied = false
      ttl     = 300
      comment = "Docker Swarm edge health endpoint; managed by Terraform"
    }
  }
}

resource "cloudflare_dns_record" "managed" {
  for_each = local.records

  zone_id = var.cloudflare_zone_id
  name    = each.value.name
  content = each.value.content
  type    = each.value.type
  proxied = each.value.proxied
  ttl     = each.value.ttl
  comment = each.value.comment

  lifecycle {
    prevent_destroy = true
  }
}
