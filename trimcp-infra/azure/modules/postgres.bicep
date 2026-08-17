param deploymentName string
param location string

// Full flexible server + private DNS + pgvector: finish in dedicated PR (Enterprise Deployment Plan §5 / Appendix I.3).
output implementationNote string = 'PostgreSQL Flexible Server: private VNet integration, azure.extensions vector+pg_trgm, Key Vault password reference — implement using network.outputs.dbSubnetId'
