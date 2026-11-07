// SPDX-License-Identifier: Apache-2.0
package org.opensearch.action.search;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import org.opensearch.common.settings.Settings;
import org.opensearch.common.util.concurrent.ThreadContext;
import org.opensearch.core.action.ActionListener;
import org.opensearch.core.tasks.TaskCancelledException;
import org.opensearch.tasks.TaskResourceTrackingService;

/** Executes the actual candidate helper against stock libraries; no mock server. */
public class LifecycleContract {
    static void check(boolean value) {
        if (!value) throw new AssertionError("lifecycle contract failed");
    }
    public static void main(String[] args) throws Exception {
        for (int mode = 0; mode < 3; mode++) {
            {
                ThreadContext context = new ThreadContext(Settings.EMPTY);
                context.addResponseHeader(TaskResourceTrackingService.TASK_RESOURCE_USAGE, "old-a");
                context.addResponseHeader(TaskResourceTrackingService.TASK_RESOURCE_USAGE, "old-b");
                context.addResponseHeader("X-Unrelated", "a");
                context.addResponseHeader("X-Unrelated", "b");
                context.putHeader("X-Request", "request");
                context.putTransient("transient", "value");
                AtomicInteger calls = new AtomicInteger();
                AtomicReference<Exception> failure = new AtomicReference<>();
                AtomicReference<Supplier<ThreadContext.StoredContext>> saved = new AtomicReference<>();
                Runnable boundary = () -> {
                    calls.incrementAndGet();
                    check(!context.getResponseHeaders().containsKey(TaskResourceTrackingService.TASK_RESOURCE_USAGE));
                    check(context.getResponseHeaders().get("X-Unrelated").equals(List.of("a", "b")));
                    check("request".equals(context.getHeader("X-Request")));
                    check("value".equals(context.getTransient("transient")));
                    saved.set(context.newRestorableContext(true));
                };
                ActionListener<SearchResponse> listener = TransportSearchScrollAction.clearScrollResourceUsage(
                    context, ActionListener.wrap(response -> { check(response == null); boundary.run(); },
                    error -> { failure.set(error); boundary.run(); }));
                Exception expected = mode == 1 ? new IllegalArgumentException("failure") : new TaskCancelledException("cancelled");
                if (mode == 0) listener.onResponse(null); else listener.onFailure(expected);
                check(calls.get() == 1);
                check(mode == 0 ? failure.get() == null : failure.get() == expected);
                try (ThreadContext.StoredContext changed = context.stashContext();
                     ThreadContext.StoredContext restored = saved.get().get()) {
                    check(!context.getResponseHeaders().containsKey(TaskResourceTrackingService.TASK_RESOURCE_USAGE));
                    check(context.getResponseHeaders().get("X-Unrelated").equals(List.of("a", "b")));
                }
            }
        }
        System.out.println("PASS: success, failure, cancellation callback, context capture, unrelated state");
    }
}
