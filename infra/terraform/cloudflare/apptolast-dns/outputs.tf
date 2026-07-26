output "managed_records" {
  description = "DNS names and record identifiers managed by this root."
  value = {
    for key, record in cloudflare_dns_record.managed :
    key => {
      id   = record.id
      name = record.name
    }
  }
}
