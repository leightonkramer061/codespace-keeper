#define _GNU_SOURCE
#include <pwd.h>
#include <sys/types.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <dlfcn.h>

static struct passwd fake_pw;
static char fake_name[64];
static char fake_dir[512];
static char fake_shell[] = "/bin/bash";
static char fake_passwd[] = "x";

struct passwd *getpwuid(uid_t uid) {
    struct passwd *(*orig_getpwuid)(uid_t) = (struct passwd *(*)(uid_t))dlsym(RTLD_NEXT, "getpwuid");
    struct passwd *res = orig_getpwuid ? orig_getpwuid(uid) : NULL;
    if (res) return res;

    snprintf(fake_name, sizeof(fake_name), "user%d", (int)uid);
    const char *home = getenv("HOME");
    if (!home || !*home) home = "/tmp";
    strncpy(fake_dir, home, sizeof(fake_dir) - 1);
    fake_dir[sizeof(fake_dir) - 1] = 0;

    fake_pw.pw_name = fake_name;
    fake_pw.pw_passwd = fake_passwd;
    fake_pw.pw_uid = uid;
    fake_pw.pw_gid = getgid();
    fake_pw.pw_gecos = fake_name;
    fake_pw.pw_dir = fake_dir;
    fake_pw.pw_shell = fake_shell;
    return &fake_pw;
}

int getpwuid_r(uid_t uid, struct passwd *pwd, char *buf, size_t buflen, struct passwd **result) {
    int (*orig_getpwuid_r)(uid_t, struct passwd *, char *, size_t, struct passwd **) =
        (int (*)(uid_t, struct passwd *, char *, size_t, struct passwd **))dlsym(RTLD_NEXT, "getpwuid_r");
    int res = orig_getpwuid_r ? orig_getpwuid_r(uid, pwd, buf, buflen, result) : -1;
    if (res == 0 && result && *result) return 0;

    struct passwd *fake = getpwuid(uid);
    if (!fake || !pwd || !result) {
        if (result) *result = NULL;
        return 0;
    }
    *pwd = *fake;
    *result = pwd;
    return 0;
}

struct passwd *getpwnam(const char *name) {
    struct passwd *(*orig_getpwnam)(const char *) = (struct passwd *(*)(const char *))dlsym(RTLD_NEXT, "getpwnam");
    struct passwd *res = orig_getpwnam ? orig_getpwnam(name) : NULL;
    if (res) return res;

    uid_t uid = getuid();
    snprintf(fake_name, sizeof(fake_name), "%s", name ? name : "user");
    const char *home = getenv("HOME");
    if (!home || !*home) home = "/tmp";
    strncpy(fake_dir, home, sizeof(fake_dir) - 1);
    fake_dir[sizeof(fake_dir) - 1] = 0;

    fake_pw.pw_name = fake_name;
    fake_pw.pw_passwd = fake_passwd;
    fake_pw.pw_uid = uid;
    fake_pw.pw_gid = getgid();
    fake_pw.pw_gecos = fake_name;
    fake_pw.pw_dir = fake_dir;
    fake_pw.pw_shell = fake_shell;
    return &fake_pw;
}

int getpwnam_r(const char *name, struct passwd *pwd, char *buf, size_t buflen, struct passwd **result) {
    int (*orig_getpwnam_r)(const char *, struct passwd *, char *, size_t, struct passwd **) =
        (int (*)(const char *, struct passwd *, char *, size_t, struct passwd **))dlsym(RTLD_NEXT, "getpwnam_r");
    int res = orig_getpwnam_r ? orig_getpwnam_r(name, pwd, buf, buflen, result) : -1;
    if (res == 0 && result && *result) return 0;

    struct passwd *fake = getpwnam(name);
    if (!fake || !pwd || !result) {
        if (result) *result = NULL;
        return 0;
    }
    *pwd = *fake;
    *result = pwd;
    return 0;
}
