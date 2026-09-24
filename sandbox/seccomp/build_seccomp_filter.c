/* Builds the BPF blocklist used by sandbox/run.sh (`bwrap --seccomp FD`). */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <seccomp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

/* Syscalls denied with EPERM, grouped by what they'd buy an attacker already
 * executing code inside the sandbox -- the assumed starting position. */
static const char *const BLOCKED[] = {
    /* Load code into the kernel -- a browser never loads modules. */
    "init_module", "finit_module", "delete_module",
    "create_module", "get_kernel_syms", "query_module",

    /* Replace or halt the running kernel. */
    "kexec_load", "kexec_file_load", "reboot",

    /* Direct hardware / raw x86 port I/O. */
    "ioperm", "iopl",

    /* Swap, quota, process accounting. */
    "swapon", "swapoff", "quotactl", "quotactl_fd", "acct",

    /* System clock: moving it backwards attacks cert expiry and HSTS/pin
     * lifetimes -- directly on the browser's own security model. Reading
     * the clock is untouched. */
    "settimeofday", "stime", "adjtimex",
    "clock_settime", "clock_settime64",
    "clock_adjtime", "clock_adjtime64",

    /* Mount plumbing: bwrap finishes its own mounts before this filter is
     * installed, but Firefox's content/utility-process sandbox legitimately
     * calls mount() and pivot_root() afterward inside a namespace it creates
     * for itself -- see header comment, both are deliberately absent from
     * this list. Everything else here stays blocked: remounting/unmounting
     * existing mounts, the modern mount API (fsopen/fsconfig/fsmount/
     * move_mount), and setns (the "jump into another namespace" primitive).
     * chroot is also intentionally absent -- see header comment. */
    "mount_setattr", "umount", "umount2",
    "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick",
    "setns",

    /* File handles: open_by_handle_at resolves a handle against the
     * underlying filesystem, ignoring the mount namespace -- the classic
     * container escape when combined with a leaked handle. */
    "name_to_handle_at", "open_by_handle_at",

    /* Kernel keyring: session/user keys, a historically rich LPE source. */
    "add_key", "request_key", "keyctl",

    /* Tracing / profiling / cross-process access. ptrace: nothing in a
     * browser needs it, and it's how a compromised process would inject
     * into siblings (cost: crashpad can no longer attach, so crash reports
     * degrade -- drop "ptrace" here to get that back). pidfd_getfd steals
     * fds from another process. perf_event_open/bpf: large kernel attack
     * surface the browser doesn't use. */
    "ptrace", "process_vm_writev", "pidfd_getfd",
    "perf_event_open", "bpf", "lookup_dcookie",

    /* Exotic memory manipulation: userfaultfd is the standard kernel-exploit
     * heap-grooming primitive; vmsplice has its own CVE history (Dirty
     * Pipe family). Ordinary splice() stays allowed -- used on real paths. */
    "userfaultfd", "vmsplice", "remap_file_pages",

    /* io_uring: a parallel syscall surface reachable from a single ring,
     * with a steady stream of LPEs and known ability to sidestep seccomp
     * policies that only inspect syscall entry. Chromium doesn't use it. */
    "io_uring_setup", "io_uring_enter", "io_uring_register",

    /* Kernel log: syslog() leaks addresses/module layout, KASLR-defeating. */
    "syslog",

    /* Obsolete / never used. */
    "uselib", "ustat", "sysfs", "nfsservctl", "vm86", "vm86old", "_sysctl",
};

/* socket(2) address families denied with EPERM -- independent kernel
 * protocol stacks reachable by an unprivileged process. A browser needs
 * only AF_UNIX, AF_INET, AF_INET6, and AF_NETLINK (interface-change
 * notifications); everything else here is blocked, including families like
 * AF_ALG/AF_VSOCK/AF_XDP/AF_CAN that need no capability, so dropping caps
 * alone doesn't cover them. AF_BLUETOOTH is blocked because Web Bluetooth
 * reaches BlueZ over D-Bus, not a raw socket. */
static const struct { const char *name; int family; } BLOCKED_FAMILIES[] = {
    { "AF_AX25",       3 }, { "AF_IPX",        4 }, { "AF_APPLETALK",  5 },
    { "AF_NETROM",     6 }, { "AF_ATMPVC",     8 }, { "AF_X25",        9 },
    { "AF_ROSE",      11 }, { "AF_DECnet",    12 }, { "AF_NETBEUI",   13 },
    { "AF_SECURITY",  14 }, { "AF_KEY",       15 }, { "AF_PACKET",    17 },
    { "AF_ATMSVC",    20 }, { "AF_RDS",       21 }, { "AF_PPPOX",     24 },
    { "AF_LLC",       26 }, { "AF_CAN",       29 }, { "AF_TIPC",      30 },
    { "AF_BLUETOOTH", 31 }, { "AF_RXRPC",     33 }, { "AF_ISDN",      34 },
    { "AF_ALG",       38 }, { "AF_NFC",       39 }, { "AF_VSOCK",     40 },
    { "AF_QIPCRTR",   42 }, { "AF_SMC",       43 }, { "AF_XDP",       44 },
    { "AF_MCTP",      45 },
};

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <output.bpf>\n", argv[0]);
        return 2;
    }

    scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ALLOW);
    if (!ctx) {
        fprintf(stderr, "error: seccomp_init failed\n");
        return 1;
    }

    /* Add the two alternate x86 ABIs so the blocklist can't be stepped
     * around by switching ABI. Failures here are fatal: a filter silently
     * missing an ABI reads as protection that isn't actually there. */
    int rc;
    if ((rc = seccomp_arch_add(ctx, SCMP_ARCH_X86)) < 0 && rc != -EEXIST) {
        fprintf(stderr, "error: could not add x86 arch to filter: %s\n", strerror(-rc));
        seccomp_release(ctx);
        return 1;
    }
    if ((rc = seccomp_arch_add(ctx, SCMP_ARCH_X32)) < 0 && rc != -EEXIST) {
        fprintf(stderr, "error: could not add x32 arch to filter: %s\n", strerror(-rc));
        seccomp_release(ctx);
        return 1;
    }

    int blocked = 0, skipped = 0;
    for (size_t i = 0; i < sizeof(BLOCKED) / sizeof(BLOCKED[0]); i++) {
        int nr = seccomp_syscall_resolve_name(BLOCKED[i]);
        if (nr == __NR_SCMP_ERROR) {
            /* Not known to this libseccomp -- doesn't exist on any ABI it
             * knows about, so there's nothing to block. Harmless. */
            printf("  skip (unknown to libseccomp): %s\n", BLOCKED[i]);
            skipped++;
            continue;
        }
        rc = seccomp_rule_add(ctx, SCMP_ACT_ERRNO(EPERM), nr, 0);
        if (rc < 0) {
            fprintf(stderr, "error: rule for %s failed: %s\n", BLOCKED[i], strerror(-rc));
            seccomp_release(ctx);
            return 1;
        }
        blocked++;
    }

    /* Argument-filtered rules on socket(2). On x86, socket() is multiplexed
     * through socketcall(), where the family is behind a pointer BPF can't
     * inspect -- seccomp_rule_add() is best-effort across architectures, so
     * it adds where it can and this reports what got skipped. */
    int families = 0;
    int sock_nr = seccomp_syscall_resolve_name("socket");
    if (sock_nr == __NR_SCMP_ERROR) {
        fprintf(stderr, "warning: cannot resolve socket(); address families not filtered\n");
    } else {
        for (size_t i = 0; i < sizeof(BLOCKED_FAMILIES) / sizeof(BLOCKED_FAMILIES[0]); i++) {
            rc = seccomp_rule_add(ctx, SCMP_ACT_ERRNO(EPERM), sock_nr, 1,
                                  SCMP_A0(SCMP_CMP_EQ,
                                          (scmp_datum_t)BLOCKED_FAMILIES[i].family));
            if (rc < 0) {
                fprintf(stderr, "warning: socket(%s) rule skipped: %s\n",
                        BLOCKED_FAMILIES[i].name, strerror(-rc));
                continue;
            }
            families++;
        }
    }

    int fd = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        perror("open output");
        seccomp_release(ctx);
        return 1;
    }
    rc = seccomp_export_bpf(ctx, fd);
    if (rc < 0) {
        fprintf(stderr, "error: seccomp_export_bpf failed: %s\n", strerror(-rc));
        close(fd);
        seccomp_release(ctx);
        return 1;
    }
    close(fd);
    seccomp_release(ctx);

    printf("Wrote %s: %d syscalls blocked, %d address families blocked",
           argv[1], blocked, families);
    if (skipped)
        printf(" (%d names unknown to libseccomp, skipped)", skipped);
    printf("\n");
    return 0;
}
