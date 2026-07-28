mock_provider "cloudflare" {}

override_resource {
  target = cloudflare_dns_record.managed["kropia"]
}

override_resource {
  target = cloudflare_dns_record.managed["minecraft-stats"]
}

override_resource {
  target = cloudflare_dns_record.managed["minecraft"]
}

override_resource {
  target = cloudflare_dns_record.managed["n8n"]
}

override_resource {
  target = cloudflare_dns_record.managed["openclaw"]
}

override_resource {
  target = cloudflare_dns_record.managed["passbolt"]
}

override_resource {
  target = cloudflare_dns_record.managed["generadorcodigosqr"]
}

override_resource {
  target = cloudflare_dns_record.managed["pablohurtadohg"]
}

override_resource {
  target = cloudflare_dns_record.managed["albertohidalgo"]
}

# Cubre la rama `adoption_only` del calculo de `content`. AVISO: esa rama fija
# los registros en platform_dns_legacy_ipv4, una maquina que el propietario ya
# elimino, asi que aplicarla hoy dejaria el dominio entero sin servicio. Se
# conserva unicamente como prueba unitaria del camino de codigo.
run "adoption_keeps_application_traffic_on_legacy" {
  command = plan

  variables {
    adoption_only = true
  }

  assert {
    condition = (
      local.cloudflare_account_id == "becc1c00820a0f6779e9b278ab150abf" &&
      local.cloudflare_zone_id == "b69e21ff397bd00c35b989fe10068d0e" &&
      terraform_data.root_identity.input.root ==
      "cloudflare/apptolast-dns"
    )
    error_message = "The Cloudflare identifiers differ from the shared contract."
  }

  assert {
    condition = toset(keys(cloudflare_dns_record.managed)) == toset([
      "edge",
      "kropia",
      "minecraft-stats",
      "minecraft",
      "n8n",
      "openclaw",
      "passbolt",
      "generadorcodigosqr",
      "pablohurtadohg",
      "albertohidalgo",
    ])
    error_message = "Terraform must manage exactly the ten authorized DNS records."
  }

  assert {
    condition = alltrue([
      for key, record in cloudflare_dns_record.managed :
      record.name == "${key}.apptolast.com" &&
      record.zone_id == "b69e21ff397bd00c35b989fe10068d0e" &&
      record.content == (
        key == "edge" ? "159.195.156.57" : "138.199.157.58"
      ) &&
      record.type == "A" &&
      record.proxied == false &&
      record.ttl == 300
    ])
    error_message = "Adoption must keep every existing application record on the legacy server."
  }
}

# El corte ya ocurrio: las diez etiquetas resuelven a la IP de la plataforma y
# el servidor legado fue eliminado. Este bloque fija ese estado real, de modo
# que un plan sobre la raiz sea un no-op y no una reversion masiva hacia una
# maquina inexistente.
run "post_cutover_contract" {
  command = plan

  assert {
    condition = alltrue([
      for key, record in cloudflare_dns_record.managed :
      record.name == "${key}.apptolast.com" &&
      record.zone_id == "b69e21ff397bd00c35b989fe10068d0e" &&
      record.content == "159.195.156.57" &&
      record.type == "A" &&
      record.proxied == false &&
      record.ttl == 300
    ])
    error_message = "A managed Cloudflare record differs from the reviewed post-cutover contract."
  }

  assert {
    condition = alltrue([
      for key, enabled in local.platform_contract.platform_dns_cutover :
      enabled == true
    ])
    error_message = "Every reviewed DNS label must declare the cutover that already happened."
  }

  assert {
    condition = (
      local.platform_contract.platform_dns_cutover.minecraft == true &&
      cloudflare_dns_record.managed["minecraft"].content ==
      local.platform_contract.platform_public_ipv4 &&
      local.platform_contract.platform_minecraft_public_enabled == true &&
      local.platform_contract
      .platform_minecraft_offline_public_accepted == true
    )
    error_message = "Publishing Minecraft requires its ingress, DNS and offline-mode acceptance gates to agree."
  }

  assert {
    condition = (
      local.minecraft_contract.minecraft_contract.online_mode == true ||
      local.platform_contract
      .platform_minecraft_offline_public_accepted == true
    )
    error_message = "An offline-mode Minecraft server may only be published with a recorded owner acceptance."
  }
}
