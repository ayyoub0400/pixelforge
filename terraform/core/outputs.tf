output "ecr_api_url" {
  description = "api container repo"
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_worker_url" {
  description = "worker container repo"
  value       = aws_ecr_repository.worker.repository_url
}

