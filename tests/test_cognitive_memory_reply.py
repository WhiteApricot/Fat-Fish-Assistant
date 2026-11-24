import asyncio
import unittes

from fat_fish.memory.reply_guard import (
    Delivery, Dependency, PolicyFence, ProtectedReply, ReplyStamp, UNAVAILABLE_REPLY,
)


class CognitiveMemoryReplyTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, *, changes, allowed=True, transport_lost=False):
        memory = {1: "old private excerpt", 2: "independent authorized excerpt"}
        submitted = []
        requests = []
        effects = []
        fence = PolicyFence()

        async def build(original, *, allow_tools):
            request = (original, tuple(memory.values()), allow_tools)
            requests.append(request)
            stamp = ReplyStamp("turn", "group", 1, (), 0, 1, tuple(
                Dependency(key, 1, value) for key, value in memory.items()
            ))
            return request, stamp

        def valid(stamp):
            return all(memory.get(dep.item_id) == dep.evidence_digest for dep in stamp.dependencies)

        async def generate(request):
            if request[2]:
                effects.append("once")
            for part in request[1]:
                yield par
            if changes:
                key = changes.pop(0)
                fence.revoke(lambda: memory.pop(key))

        async def submit(draft, authorize_handoff):
            def send():
                submitted.append(draft)
                return Delivery.SUBMITTED

            result = authorize_handoff(send)
            if transport_lost and result == Delivery.SUBMITTED:
                raise OSError("Adapter result unknown")
            return resul

        guard = ProtectedReply(fence, lambda: allowed, valid)
        outcome = await guard.run("original request", build, generate, submit,
                                  initial_deadline=asyncio.get_running_loop().time() + 1)
        return outcome, requests, effects, submitted

    async def test_regeneration_keeps_independent_evidence_and_does_not_repeat_tools(self):
        outcome, requests, effects, submitted = await self.run_scenario(changes=[1])
        self.assertEqual(outcome, Delivery.SUBMITTED)
        self.assertEqual(effects, ["once"])
        self.assertEqual(requests[-1], ("original request", ("independent authorized excerpt",), False))
        self.assertEqual(submitted, ["independent authorized excerpt"])

    async def test_second_revocation_falls_back(self):
        outcome, requests, effects, submitted = await self.run_scenario(changes=[1, 2])
        self.assertEqual(outcome, Delivery.SUBMITTED)
        self.assertEqual(len(requests), 2)
        self.assertEqual(effects, ["once"])
        self.assertEqual(submitted, [UNAVAILABLE_REPLY])

    async def test_transport_unknown_does_not_resend(self):
        outcome, requests, effects, submitted = await self.run_scenario(changes=[], transport_lost=True)
        self.assertEqual(outcome, Delivery.UNKNOWN)
        self.assertEqual((len(requests), len(effects), len(submitted)), (1, 1, 1))

    async def test_revoked_destination_never_sends(self):
        self.assertEqual(await self.run_scenario(changes=[], allowed=False),
                         (Delivery.REVOKED, [], [], []))
