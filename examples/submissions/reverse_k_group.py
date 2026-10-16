"""Reference example; ListNode is supplied by the container harness."""


class Solution:
    def reverseKGroup(self, head, k):
        dummy = type(head)(0, head)
        before = dummy
        while True:
            end = before
            for _ in range(k):
                end = end.next
                if end is None:
                    return dummy.next
            after = end.next
            previous, current = after, before.next
            while current is not after:
                following = current.next
                current.next = previous
                previous, current = current, following
            old_start = before.next
            before.next = end
            before = old_start
