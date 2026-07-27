locals {
  records = {
    edge = {
      name    = local.platform_contract.edge_traefik_hostname
      cutover = local.platform_contract.platform_dns_cutover.edge
    }
    kropia = {
      name    = "kropia.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.kropia
    }
    minecraft-stats = {
      name = "minecraft-stats.${local.platform_contract.edge_cloudflare_zone}"
      cutover = (
        local.platform_contract.platform_dns_cutover["minecraft-stats"]
      )
    }
    minecraft = {
      name    = "minecraft.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.minecraft
    }
    n8n = {
      name    = "n8n.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.n8n
    }
    openclaw = {
      name    = "openclaw.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.openclaw
    }
    passbolt = {
      name    = "passbolt.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.passbolt
    }
    generadorcodigosqr = {
      name    = "generadorcodigosqr.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.generadorcodigosqr
    }
    pablohurtadohg = {
      name    = "pablohurtadohg.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.pablohurtadohg
    }
    albertohidalgo = {
      name    = "albertohidalgo.${local.platform_contract.edge_cloudflare_zone}"
      cutover = local.platform_contract.platform_dns_cutover.albertohidalgo
    }
  }
}

resource "cloudflare_dns_record" "managed" {
  for_each = local.records

  depends_on = [terraform_data.root_identity]

  zone_id = local.cloudflare_zone_id
  name    = each.value.name
  content = (
    each.key == "edge"
    ? local.platform_contract.platform_public_ipv4
    : (
      var.adoption_only
      ? local.platform_contract.platform_dns_legacy_ipv4
      : (
        each.value.cutover
        ? local.platform_contract.platform_public_ipv4
        : local.platform_contract.platform_dns_legacy_ipv4
      )
    )
  )
  type    = "A"
  proxied = false
  ttl     = 300

  lifecycle {
    prevent_destroy = true
  }
}
