set -euo pipefail
JDK=/usr/share/opensearch/jdk/bin
LIB=/usr/share/opensearch/lib
"$JDK/java" -version 2> /work/jdk.txt
cp "$LIB/opensearch-2.19.0.jar" /work/stock.jar
for variant in baseline candidate; do
  mkdir /work/$variant/classes
  "$JDK/javac" --release 11 -proc:none -cp "$LIB/*" -d /work/$variant/classes /work/$variant/server/src/main/java/org/opensearch/action/search/TransportSearchScrollAction.java
done
"$JDK/javac" --release 11 -proc:none -cp "/work/candidate/classes:$LIB/*" -d /work/test-classes /work/LifecycleContract.java
"$JDK/java" -ea -cp "/work/test-classes:/work/candidate/classes:$LIB/*" org.opensearch.action.search.LifecycleContract > /work/contract.txt
