output "ecr_full" { value = aws_ecr_repository.full.repository_url }
output "ecr_assembler" { value = aws_ecr_repository.assembler.repository_url }
output "ecr_gateway" { value = aws_ecr_repository.gateway.repository_url }
output "s3_audio" { value = aws_s3_bucket.audio.bucket }
output "s3_output" { value = aws_s3_bucket.output.bucket }
output "sqs_dub_tasks" { value = aws_sqs_queue.dub_tasks.url }
output "sqs_dlq" { value = aws_sqs_queue.dlq.url }
output "node_role" { value = aws_iam_role.node.arn }
