output "firewall_policy_id" {
  description = "Netcup firewall policy identifier, or null while disabled."
  value       = try(netcup_firewall_policy.host_ingress[0].id, null)
}

output "scp_ssh_key_ids" {
  description = "SCP identifiers for managed public installation keys."
  value       = { for key, item in netcup_ssh_key.image_installation : key => item.id }
}
