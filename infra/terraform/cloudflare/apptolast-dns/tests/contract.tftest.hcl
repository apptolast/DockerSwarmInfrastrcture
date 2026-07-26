mock_provider "cloudflare" {}

run "shared_contract" {
  command = plan

  variables {
    cloudflare_zone_id = "00000000000000000000000000000000"
  }

  assert {
    condition = (
      cloudflare_dns_record.managed["edge"].name == "edge.apptolast.com" &&
      cloudflare_dns_record.managed["edge"].content == "159.195.156.57" &&
      cloudflare_dns_record.managed["edge"].type == "A" &&
      cloudflare_dns_record.managed["edge"].proxied == false
    )
    error_message = "The Cloudflare resource differs from the shared contract."
  }
}
