locals {
  platform_contract = yamldecode(
    file("${path.module}/../../../../config/platform.yml")
  )
}

check "platform_contract" {
  assert {
    condition = (
      can(regex(
        "^([0-9]{1,3}\\.){3}[0-9]{1,3}$",
        local.platform_contract.platform_public_ipv4
      )) &&
      can(cidrhost(
        "${local.platform_contract.platform_public_ipv4}/32",
        0
      )) &&
      can(regex(
        "^[a-z0-9.-]+$",
        local.platform_contract.edge_traefik_hostname
      )) &&
      endswith(
        local.platform_contract.edge_traefik_hostname,
        ".${local.platform_contract.edge_cloudflare_zone}"
      )
    )
    error_message = "The shared edge IPv4 or hostname contract is invalid."
  }
}
