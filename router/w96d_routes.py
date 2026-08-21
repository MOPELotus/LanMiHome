"""W96D HTTP route integration.

Keeps W96D independent from existing Xiaomi MIoT routes.
"""


def register_w96d_routes(app, runtime):
    @app.get('/api/v1/w96d')
    def get_w96d():
        return runtime.state()

    @app.post('/api/v1/w96d')
    def patch_w96d(body: dict):
        return runtime.control(body)
