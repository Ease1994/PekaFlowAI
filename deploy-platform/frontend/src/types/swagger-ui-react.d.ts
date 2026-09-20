declare module 'swagger-ui-react' {
  import type { ComponentType } from 'react'

  type SwaggerRequest = { headers?: Record<string, string> }

  type SwaggerUIProps = {
    spec?: Record<string, unknown>
    persistAuthorization?: boolean
    filter?: boolean | string
    tryItOutEnabled?: boolean
    docExpansion?: 'list' | 'full' | 'none'
    defaultModelsExpandDepth?: number
    operationsSorter?: 'alpha' | 'method'
    requestInterceptor?: (req: SwaggerRequest) => SwaggerRequest
  }

  const SwaggerUI: ComponentType<SwaggerUIProps>
  export default SwaggerUI
}

declare module 'swagger-ui-react/swagger-ui.css'
