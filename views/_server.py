"""views가 server.py의 이름을 늦게 찾아 쓰게 하는 통로.
server.py가 시작할 때 bind(자기 모듈)를 부른다. `python3 server.py`로 띄우면 모듈 이름이 __main__이라 `import server`를 쓸 수 없고, 테스트가 server의 이름을 patch로 바꿔 끼워도 먹어야 하므로 호출 시점에 찾는다."""


class _Server:
    module = None

    def __getattr__(self, name):
        return getattr(self.module, name)


S = _Server()


def bind(module) -> None:
    S.module = module
